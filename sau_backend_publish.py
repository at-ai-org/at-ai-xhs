# -*- coding: utf-8 -*-
"""发布中心后端：多平台批量发布的「实时进度 + 发布记录」（at-ai-xhs 改造新增）。

为什么不让前端继续直接调 `/postVideo`
-------------------------------------
上游的 `/postVideo` 是「一个平台一个请求、同步跑完才返回」：前端只能显示"已提交"，
中途失败连是哪条素材、哪个账号都说不清，而且**发过什么都没留痕**。

这里的做法：

- 前端一次提交整批（素材 × 平台 × 账号），后端拆成**每个账号每个素材一个任务**，
  逐个执行，状态实时可查；
- 每个任务在**提交的那一刻**就落一条 `publish_records`，所以"发过的"一定有记录，
  哪怕进程中途被杀（重启时会把残留的 running/queued 标成「已中断」）；
- 任务的实时进度由执行器显式上报 + 上传器日志旁听两条腿拼出来，
  见 `myUtils/publish_progress.py`；小红书还能额外读到页面操作层的逐步结果；
- 浏览器操作仍然是**全局串行**的（`myUtils/browser_lock.py` 那把锁，与小红书工作台共用）：
  同账号多会话会互相踢下线，宁可排队。

接口一览
--------
```
POST /publish/submit              创建发布批次 → { job_id, tasks[] }
GET  /publish/job/<job_id>        批次实时进度（平台 / 账号 / 任务三层）
GET  /publish/task/<task_id>      单个任务详情（含逐步结果 + 完整日志）
GET  /publish/jobs?active=1       最近的批次（刷新页面后据此恢复进度视图）
POST /publish/task/<task_id>/cancel 中断**单个**任务（排队中的 / 正在跑的都能中断）
POST /publish/job/<job_id>/cancel 取消批次里还没开始的任务（带 running=1 连跑着的一起中断）
GET  /publish/records             发布记录（分页 + 筛选 + 统计）
GET  /publish/records/<record_id> 单条记录详情
POST /publish/check-published     这批「素材 × 平台 × 账号」里哪些已经发过了（二次确认用）
```

中断（2026-09 新增）
--------------------
"有些任务太慢了一直卡着"没法等 —— 所以每个任务都能手动中断，见 `_cancel_task`。
两条腿一起走：

1. **协作式中断**：任务线程拿到一个 `threading.Event`，在排队等锁、等并发名额等
   自己控制的等待点上回头看一眼，被中断就抛 `PublishCancelled` 干净退出；
2. **异步中断**：上传器是 `async` 的，执行器自己建事件循环并把 `(loop, Task)` 登记出来，
   HTTP 线程用 `loop.call_soon_threadsafe(task.cancel)` 把 `CancelledError` 抛进
   正在 await 的协程里 —— 这一步能真的打断"卡在某个 await 上"的任务。

**但结论不能等线程**：浏览器驱动不响应时它可能永远不 unwind。所以中断接口是
**先给结论、再放通道**（复用看门狗那套：结论定死、迟到的收尾不许改写、
通道立刻去跑下一个任务），而不是等线程自己收尾。

已知限制
--------
* 中断**不保证**线程真的停下：它可能被压在浏览器里（等一个不再响应的驱动）永不返回。
  能保证的是：结论是对的（「已取消」）、通道不堵、界面上不会一直转圈。
* 这种线程**还占着**账号锁和并发名额，之后同账号的任务会先卡在等锁上，
  直到 `LOCK_WAIT_TIMEOUT` 超时。要彻底回收资源还是得重启后端。
* 上游上传器里有若干无上限的 `while True` 等待（等页面跳转、等上传完成），
  这类等待没有回头看一眼中断标志的地方，只能靠异步中断那条腿。

重复发布的二次确认（2026-09 新增）
----------------------------------
同一个账号把**同一条素材**再发一次，多半是手滑（素材还留在选择列表里就又点了一遍
「开始发布」），但也可能是用户真的想重发——所以不能拦死，只能**让人明确确认一次**：

* 判定数据在 `myUtils/publish_records.find_repeat_publishes`（只查发布记录，不下结论）；
* 前端点「开始发布」时先调 `/publish/check-published`，有重复就把明细列出来让人确认；
* 确认后才提交，`options.confirmRepublish=true` 跟过来，提交时给那些重复的任务补一行
  日志——事后翻发布记录能看出"当时是知情重发"，而不是"系统压根没提过"。

`/publish/submit` 本身**不拦**重复发布（有意的）：重发是合法操作，挡在服务端只会让
"确认过了还要再确认一次"变成死循环。这里只做一件事——别让人在不知情的情况下发重。
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request

import myUtils.publish_progress as progress
import myUtils.publish_prompt as prompts
import myUtils.app_settings as settings
from conf import BASE_DIR
from myUtils import publish_records as records
from myUtils.browser_lock import (
    MAX_CONCURRENT_BROWSERS,
    BrowserSessionCancelled,
    browser_session,
)
from uploader.douyin_uploader.main import (
    DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
    DOUYIN_PUBLISH_STRATEGY_SCHEDULED,
    DouYinVideo,
)
from uploader.ks_uploader.main import (
    KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE,
    KUAISHOU_PUBLISH_STRATEGY_SCHEDULED,
    KSVideo,
)
from uploader.pdd_uploader.main import (
    PDD_PUBLISH_STRATEGY_IMMEDIATE,
    PDD_PUBLISH_STRATEGY_SCHEDULED,
    PddVideo,
)
from uploader.tencent_uploader.main import (
    TENCENT_PUBLISH_STRATEGY_IMMEDIATE,
    TENCENT_PUBLISH_STRATEGY_SCHEDULED,
    TencentVideo,
)
from uploader.xiaohongshu_uploader.main import (
    XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
    XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED,
    XiaoHongShuVideo,
)
from utils.files_times import generate_schedule_time_next_day

publish_bp = Blueprint("publish", __name__, url_prefix="/publish")

# --------------------------------------------------------------------------- #
# 平台定义
# --------------------------------------------------------------------------- #

PLATFORMS: dict[int, dict] = {
    1: {"key": 1, "name": "小红书", "business": "xiaohongshu"},
    2: {"key": 2, "name": "视频号", "business": "tencent"},
    3: {"key": 3, "name": "抖音", "business": "douyin"},
    4: {"key": 4, "name": "快手", "business": "kuaishou"},
    # 5 = 拼多多视频（多多视频）。账号身份固定是「达人」——拼多多后台三种身份
    # （商家/达人/机构）登录方式各不相同，只有达人是扫码，见 myUtils/login.py。
    5: {"key": 5, "name": "拼多多视频", "business": "pdd"},
}

# (立即发布, 定时发布) 两个策略常量。每个平台都得显式传：
# 上传器的 `validate_base_args` 在策略为 immediate 时会把 publish_date 清零，
# 默认值又都是 immediate——定时发布如果不显式给 scheduled，时间会被悄悄丢掉。
PUBLISH_STRATEGY: dict[int, tuple[str, str]] = {
    1: (XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE, XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED),
    2: (TENCENT_PUBLISH_STRATEGY_IMMEDIATE, TENCENT_PUBLISH_STRATEGY_SCHEDULED),
    3: (DOUYIN_PUBLISH_STRATEGY_IMMEDIATE, DOUYIN_PUBLISH_STRATEGY_SCHEDULED),
    4: (KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE, KUAISHOU_PUBLISH_STRATEGY_SCHEDULED),
    5: (PDD_PUBLISH_STRATEGY_IMMEDIATE, PDD_PUBLISH_STRATEGY_SCHEDULED),
}

# --------------------------------------------------------------------------- #
# 状态与阶段
# --------------------------------------------------------------------------- #
#
# 状态词、来源、发布记录表的读写都在 `myUtils/publish_records.py`——
# 小红书工作台也往同一张表里写，两边共用一份实现，免得状态叫法慢慢跑偏。

STATUS_QUEUED = records.STATUS_QUEUED
STATUS_RUNNING = records.STATUS_RUNNING
STATUS_SUCCESS = records.STATUS_SUCCESS
STATUS_FAILED = records.STATUS_FAILED
STATUS_CANCELLED = records.STATUS_CANCELLED
STATUS_INTERRUPTED = records.STATUS_INTERRUPTED
STATUS_TEXT = records.STATUS_TEXT
FINISHED_STATUSES = records.FINISHED_STATUSES

# 小红书页面操作层的步骤名 → 阶段（比日志推断更准，能拿到就用它）
XHS_STEP_STAGE: dict[str, str] = {
    "open_publish_page": progress.STAGE_LAUNCHING,
    "select_video_tab": progress.STAGE_LAUNCHING,
    "upload_video": progress.STAGE_UPLOADING,
    "wait_upload_done": progress.STAGE_UPLOADING,
    "fill_title": progress.STAGE_FILLING,
    "fill_desc": progress.STAGE_FILLING,
    "fill_tags": progress.STAGE_FILLING,
    "select_collection": progress.STAGE_FILLING,
    "set_thumbnail": progress.STAGE_FILLING,
    "set_original": progress.STAGE_SUBMITTING,
    "set_visibility": progress.STAGE_SUBMITTING,
    "set_schedule": progress.STAGE_SUBMITTING,
    "submit": progress.STAGE_SUBMITTING,
    "verify_published": progress.STAGE_VERIFYING,
}

LOG_LIMIT = 400                      # 内存里每个任务保留的日志行数
LOG_TAIL_FOR_DB = records.LOG_TAIL_LINES   # 落库的日志行数（记录详情里能翻到）
LOG_LINE_MAX = records.LOG_LINE_MAX        # 单行最长字符数
MAX_ITEMS = 50             # 一次批次最多几条素材
MAX_TASKS_PER_JOB = 200    # 一次批次最多几个任务（素材 × 平台 × 账号）

#: 单个任务的执行上限（秒）。正常发布几分钟内必然结束，这么久还没结束就是卡住了。
#:
#: 为什么需要看门狗：底层是**阻塞式**浏览器自动化，"卡住"（等一把永不释放的锁、
#: 等一个不再响应的驱动）既不抛异常也不超时——任务会永远停在「发布中」，批次也永远
#: 不 done，界面上就是"一直卡着"。看门狗**杀不掉**那个线程（Python 做不到），但能把
#: 结论改对：任务标失败、批次能收尾、界面上给出明确原因，而不是无限转圈。
#: 用 SAU_PUBLISH_TASK_TIMEOUT 覆盖（秒）。
PUBLISH_TASK_TIMEOUT = float(
    os.environ.get("SAU_PUBLISH_TASK_TIMEOUT") or 30 * 60
)


class PublishCancelled(RuntimeError):
    """任务被用户手动中断（协作式中断那条腿抛的就是它）。

    刻意**不**直接往上抛 `asyncio.CancelledError`：它继承的是 `BaseException`，
    `_run_task` 里的 `except Exception` 接不住 —— 那样任务会跳过收尾直接冒出去，
    批次永远不 done，界面上又是另一个"一直卡着"。`_run_uploader_async` 负责把它
    翻译成本类型。
    """


# --------------------------------------------------------------------------- #
# 发布记录（薄封装，实现见 myUtils/publish_records.py）
# --------------------------------------------------------------------------- #


def _now_text(ts: float | None = None) -> str:
    return records.now_text(ts)


def _json_safe(value):
    return records.json_safe(value)


def _insert_record(task: dict) -> int:
    return records.insert_record(
        records.new_record(
            job_id=task["job_id"],
            task_id=task["task_id"],
            source=records.SOURCE_PUBLISH_CENTER,
            platform=task["platform"],
            platform_name=task["platform_name"],
            account_id=task["account_id"],
            account_name=task["account_name"],
            account_file=task["account_file"],
            material=task["material"],
            material_title=task["material_title"],
            title=task["title"],
            desc=task["desc"],
            tags=task["tags"],
            scheduled=bool(task["schedule_at"]),
            schedule_at=task["schedule_at"],
            is_draft=bool(task["options"].get("isDraft")),
            is_original=bool(task["options"].get("isOriginal")),
            created_at=records.now_text(task["created_at"]),
        )
    )


def _update_record(record_id: int, **fields) -> None:
    records.update_record(record_id, **fields)


# --------------------------------------------------------------------------- #
# 批次 / 任务注册表（内存态；历史靠 publish_records）
# --------------------------------------------------------------------------- #

_lock = threading.RLock()
_tasks: dict[str, dict] = {}
_jobs: dict[str, dict] = {}
#: 每个执行通道里还没跑完的任务 id，按提交顺序排 —— 用来算"我这个通道前面还有几个"
_waiting: dict[str, list[str]] = {}
#: 每个执行通道一条队列 + 一个执行线程。通道 = 一个平台的一个账号。
_queues: dict[str, "queue.Queue[str]"] = {}
_worker_lock = threading.Lock()

#: 正在跑的异步发布：任务 id → (事件循环, 上传器协程的 Task)。
#:
#: 异步中断必须走这里，不能由 HTTP 线程直接 `task.cancel()`：事件循环**不是**
#: 线程安全的，从别的线程碰它属于未定义行为（症状是随机丢事件或直接崩）。
#: `loop.call_soon_threadsafe(...)` 才是唯一的正经入口。
_async_runs: dict[str, tuple[asyncio.AbstractEventLoop, "asyncio.Task"]] = {}
_async_runs_lock = threading.Lock()


def _register_async_run(task_id: str, loop: asyncio.AbstractEventLoop, task) -> None:
    with _async_runs_lock:
        _async_runs[task_id] = (loop, task)


def _unregister_async_run(task_id: str) -> None:
    with _async_runs_lock:
        _async_runs.pop(task_id, None)


def _interrupt_async_run(task_id: str) -> bool:
    """往任务自己的事件循环里塞一个 `Task.cancel()`。返回有没有真的送出去。

    送出去了**不等于**任务就此结束：`Task.cancel()` 只是让下一个 await 点抛
    `CancelledError`。卡在不再响应的驱动上时，那个 await 可能永远回不来——
    所以调用方绝不能等它，得自己先把结论定下来（见 `_cancel_task`）。
    """
    with _async_runs_lock:
        handle = _async_runs.get(task_id)
    if handle is None:
        return False
    loop, task = handle
    try:
        loop.call_soon_threadsafe(task.cancel)
        return True
    except RuntimeError:
        # loop 已经关掉了（任务刚好收尾）：没什么可打断的了
        return False


def _task_cancel_event(task: dict) -> threading.Event:
    """取任务的中断标志，没有就补一个（老任务/测试造的裸字典都能用）。"""
    event = task.get("_cancel_event")
    if event is None:
        event = threading.Event()
        task["_cancel_event"] = event
    return event


def _channel_key(task: dict) -> str:
    """任务的执行通道：**一个平台的一个账号**。

    并发就卡在这个粒度上，原因有两个：
    - 平台之间完全独立，没有任何理由让小红书排队等抖音发完；
    - 同一个账号不能同时开两个浏览器（会互相踢下线），所以同账号的任务仍然串行。

    多账号是同一个道理：两个账号就是两条通道，各跑各的。
    整机同时开几个浏览器另有上限，见 myUtils/browser_lock.py。
    """
    return f"{int(task['platform'])}:{int(task['account_id'])}"


def _ensure_worker(channel: str) -> None:
    """按需给一个通道起执行线程（同一个通道只会有一条线程）。"""
    with _worker_lock:
        if channel in _queues:
            return
        _queues[channel] = queue.Queue()
        threading.Thread(target=_worker_loop, args=(channel,), name=f"publish-{channel}", daemon=True).start()


def _update_task(task_id: str, persist: bool = True, **fields) -> None:
    with _lock:
        task = _tasks.get(task_id)
        if task is None:
            return
        task.update(fields)
        record_id = task["record_id"]
        payload = {k: v for k, v in fields.items() if k in records.RECORD_COLUMNS}
    if persist and payload:
        _update_record(record_id, **payload)


def _finish_task(task_id: str, status: str, message: str, *, final: bool = False) -> None:
    """给任务下结论。

    `final=True` 表示**这个结论已经定死**，之后迟到的收尾一律丢弃。两类调用方需要它：

    * 看门狗判超时——底层那个线程往往还卡在原地，等它哪天真返回时不能让它把结论
      改写成"成功"，那等于骗用户说发出去了；
    * 用户手动中断——同一个道理，线程可能还活着。

    换句话说 `final` 的语义是"线程已经不可信了"，而不是"是看门狗干的"。
    """
    now = time.time()
    with _lock:
        task = _tasks.get(task_id)
        if task is None:
            return
        if task.get("_stuck") and not final:
            return
        if final:
            task["_stuck"] = True
        started = task.get("started_at") or now
        task.update(
            status=status,
            message=message,
            finished_at=now,
            duration=round(now - started, 1),
            action=message,
        )
        task.pop("_app", None)
        # 任务结束了就不该再有"等人输入"的弹窗挂着（上传器通常已经自己收尾，
        # 但进程被杀 / 超时这类路径上未必，这里兜一刀）
        task["prompt"] = None
        queue_list = _waiting.get(task.get("channel") or _channel_key(task))
        if queue_list and task_id in queue_list:
            queue_list.remove(task_id)
        steps = _json_safe(task.get("steps") or [])
        log_tail = _json_safe(task["log"][-LOG_TAIL_FOR_DB:])
        record_id = task["record_id"]
        job_id = task["job_id"]

    _update_record(
        record_id,
        status=status,
        stage=status,
        stage_text=STATUS_TEXT.get(status, status),
        action=message,
        message=message,
        finished_at=_now_text(now),
        duration=round(now - started, 1),
        steps=json.dumps(steps, ensure_ascii=False),
        log=json.dumps(log_tail, ensure_ascii=False),
    )
    _refresh_job(job_id)


def _refresh_job(job_id: str) -> None:
    """任务全部结束就把批次标记成 done（进度页据此停止轮询）。"""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        statuses = [_tasks[tid]["status"] for tid in job["task_ids"] if tid in _tasks]
        if statuses and all(s in FINISHED_STATUSES for s in statuses):
            job["status"] = "done"
            job["finished_at"] = job.get("finished_at") or time.time()


# --------------------------------------------------------------------------- #
# 单个任务的执行
# --------------------------------------------------------------------------- #


def _task_event(task_id: str, event: dict) -> None:
    """进度桥的回调：更新"当前动作 / 阶段 / 日志尾巴"。"""
    text = (event.get("text") or "").strip()
    stage = event.get("stage")
    with _lock:
        task = _tasks.get(task_id)
        if task is None:
            return
        fields: dict = {}
        if event.get("type") == "log":
            if not text:
                return
            lines = task["log"]
            # 上传器会用 debug 级日志"每 2 秒报一次平安"（"小人还在等视频上传完成"）。
            # 连续重复的行没有信息量：丢掉，也不落库。前端已经在跑秒表，
            # 看不出差别，但日志尾巴不会被同一句话刷满。
            if lines and lines[-1]["text"] == text:
                return
            lines.append({"t": _now_text(), "text": text[:LOG_LINE_MAX], "level": event.get("level", "INFO")})
            del lines[:-LOG_LIMIT]
            fields["log_count"] = task.get("log_count", 0) + 1
            fields["action"] = text[:LOG_LINE_MAX]
        elif text:
            # 显式上报带的话（"等一个并发名额"之类）也要显示成当前动作——
            # 否则界面上只看到任务卡着不动，分不清是在跑还是在等
            fields["action"] = text[:LOG_LINE_MAX]
        if stage and progress.STAGE_ORDER.get(stage, -1) > progress.STAGE_ORDER.get(task.get("stage", ""), -1):
            fields["stage"] = stage
            fields["stage_text"] = progress.STAGE_TEXT.get(stage, "")
        if not fields:
            return
        task.update(fields)
        persist = {k: v for k, v in fields.items() if k in records.RECORD_COLUMNS}
        record_id = task["record_id"]
    if persist:
        _update_record(record_id, **persist)


def _task_prompt(task_id: str, event: dict) -> None:
    """发布途中"要人搭把手"的输入桥（抖音短信验证码等）。

    上传器在需要人工输入时会阻塞等待（`myUtils/publish_prompt.py`），这里负责把那次
    请求挂到任务上：前端轮询任务/批次时就能看到有人在等输入，直接弹窗；
    用户填完 `POST /publish/prompts/<id>`，等待中的发布线程被唤醒继续跑。
    请求结束（填完 / 跳过 / 超时）时把任务上的 prompt 撤掉，弹窗自然关闭。
    """
    with _lock:
        task = _tasks.get(task_id)
        if task is None:
            return
        if event.get("type") == "prompt_open":
            task["prompt"] = event.get("prompt") or None
        elif event.get("type") == "prompt_close":
            task["prompt"] = None


def _public_prompt(task: dict) -> dict | None:
    """把任务上挂着的输入请求配上"这是哪个平台哪个账号"的现场信息。"""
    prompt = task.get("prompt")
    if not prompt:
        return None
    return {
        # 请求自己的字段（含 title="抖音需要短信验证码"）先铺开，
        # 后面的任务上下文**不能再用 title 这个名字**去覆盖它，
        # 否则弹窗标题会变成素材标题，说明不清到底要填什么。
        **prompt,
        "task_id": task["task_id"],
        "job_id": task["job_id"],
        "platform": task["platform"],
        "platform_name": task["platform_name"],
        "account_name": task["account_name"],
        "material_title": task["material_title"],
        "task_title": task["title"],
    }


def _live_steps(task: dict) -> list:
    """小红书任务：直接读页面操作层实例上的逐步结果（跑的过程中也在长）。"""
    app = task.get("_app")
    if app is None:
        return _json_safe(task.get("steps") or [])
    try:
        return _json_safe([r.to_dict() for r in getattr(app, "step_results", [])])
    except Exception:
        return _json_safe(task.get("steps") or [])


def _validate_task(task: dict) -> None:
    material = Path(BASE_DIR) / "videoFile" / task["material"]
    if not material.is_file():
        raise RuntimeError(f"素材文件不存在: {task['material']}")
    account_file = Path(BASE_DIR) / "cookiesFile" / task["account_file"]
    if not account_file.is_file():
        raise RuntimeError(f"账号 cookie 文件不存在: {task['account_file']}，请先到「账号管理」重新登录")


def _run_uploader_async(coro, task_id: str, cancel_event: threading.Event) -> None:
    """在**本线程**里新建事件循环跑上传器，并把 loop / Task 登记出去。

    为什么不用 `asyncio.run(...)`：手动中断要从 HTTP 线程把 `CancelledError` 抛进一个
    正在 await 的协程，而 `asyncio.run` 把事件循环和 Task 都关在肚子里，外面谁也够不着。
    自己建循环、自己 `create_task`，登记之后
    `loop.call_soon_threadsafe(task.cancel)` 就能在下一个 await 点打断它。

    `CancelledError`→`PublishCancelled` 的翻译也在这里：前者是 `BaseException`，
    让它冒到 `_run_task` 的 `except Exception` 之外去，任务就会跳过收尾跑掉。
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        main_task = loop.create_task(coro)
        _register_async_run(task_id, loop, main_task)
        try:
            loop.run_until_complete(main_task)
        except asyncio.CancelledError:
            raise PublishCancelled("任务已被手动中断") from None
    finally:
        # 无论怎么结束都要撤销登记：循环马上就死了，留着只会让后来者
        # 以为"这个任务还能中断"，然后对着一具尸体 call_soon_threadsafe。
        _unregister_async_run(task_id)
        asyncio.set_event_loop(None)
        # 刻意**不**补 `loop.shutdown_asyncgens()`（`asyncio.run` 会做那一步）：
        # 上传器全程用的是 `async_playwright()` 的 `__aenter__/__aexit__`，没有异步
        # 生成器；而多跑一步收尾就等于在"中断"这条路径上重新引入一个可能卡住的地方，
        # 那正是这套代码要避免的。
        try:
            loop.close()
        except Exception:
            # 被中断的协程可能留下没跑完的内部任务（浏览器连接的收尾），
            # 关循环时它们会抱怨。这不影响结论，也不该盖住真正的异常。
            pass


def _invoke_uploader(task: dict) -> None:
    """按平台构造上传器并跑完一次发布（一个素材 × 一个账号）。"""
    platform = int(task["platform"])
    file_path = str(Path(BASE_DIR) / "videoFile" / task["material"])
    account_file = str(Path(BASE_DIR) / "cookiesFile" / task["account_file"])
    publish_at = task["publish_at"]  # datetime | 0
    scheduled = publish_at != 0
    strategy = PUBLISH_STRATEGY[platform][1 if scheduled else 0]
    options = task["options"]

    # 上传器是 async 的，统一走这一层：它负责把事件循环和协程 Task 登记出去，
    # 「中断」按钮才有东西可以 cancel。四个平台都要走，否则抖音/快手的中断是假的。
    task_id = task["task_id"]
    cancel_event = _task_cancel_event(task)

    if platform == 1:
        app = XiaoHongShuVideo(
            title=task["title"],
            file_path=file_path,
            tags=task["tags"],
            publish_date=publish_at,
            account_file=account_file,
            desc=task["desc"],
            publish_strategy=strategy,
            is_original=bool(options.get("isOriginal")),
        )
        _attach_app(task["task_id"], app)
        _run_uploader_async(app.main(), task_id, cancel_event)
        task["steps"] = _json_safe([r.to_dict() for r in getattr(app, "step_results", [])])
        return

    if platform == 2:
        app = TencentVideo(
            task["title"],
            file_path,
            task["tags"],
            publish_at,
            account_file,
            None,
            bool(options.get("isDraft")),
            desc=task["desc"],
            publish_strategy=strategy,
            # 「视频标注」默认不设置（实拍视频没有 AI 生成内容，平台也不强制）；
            # 需要时在「模型设置 → 发布默认值」里显式填。
            content_label=settings.get_tencent_content_label() or None,
        )
        _run_uploader_async(app.main(), task_id, cancel_event)
        return

    if platform == 3:
        app = DouYinVideo(
            task["title"],
            file_path,
            task["tags"],
            publish_at,
            account_file,
            thumbnail_landscape_path=options.get("thumbnail") or None,
            productLink=options.get("productLink") or "",
            productTitle=options.get("productTitle") or "",
            desc=task["desc"],
            publish_strategy=strategy,
            # 「自主声明」默认不做（实拍视频没有 AI 生成内容，平台也不强制）；
            # 需要时在「模型设置 → 发布默认值」里显式填。
            declaration=settings.get_douyin_declaration() or None,
        )
        _run_uploader_async(app.douyin_upload_video(), task_id, cancel_event)
        return

    if platform == 4:
        app = KSVideo(
            task["title"],
            file_path,
            task["tags"],
            publish_at,
            account_file,
            publish_strategy=strategy,
            desc=task["desc"],
        )
        _run_uploader_async(app.main(), task_id, cancel_event)
        return

    if platform == 5:
        # 拼多多视频（达人）。默认**不挂商品**：达人挂商品要先在拼多多后台过
        # 「匹配度测试」，本版先把发布这件事跑通，挂品留到后面单独做。
        # 封面走发布中心已有的 `thumbnail` 选项（抖音也是这么用的）。
        app = PddVideo(
            task["title"],
            file_path,
            task["tags"],
            publish_at,
            account_file,
            publish_strategy=strategy,
            desc=task["desc"],
            thumbnail_path=options.get("thumbnail") or None,
        )
        _attach_app(task["task_id"], app)
        _run_uploader_async(app.main(), task_id, cancel_event)
        task["steps"] = _json_safe(getattr(app, "step_results", []) or [])
        return

    raise RuntimeError(f"不支持的平台类型: {platform}")


def _attach_app(task_id: str, app) -> None:
    with _lock:
        if task_id in _tasks:
            _tasks[task_id]["_app"] = app


def _watchdog_message() -> str:
    minutes = PUBLISH_TASK_TIMEOUT / 60
    shown = f"{minutes:.0f}" if minutes >= 1 else f"{PUBLISH_TASK_TIMEOUT:.0f} 秒"
    return (
        f"执行超时：超过 {shown} 分钟没有结束，已判定卡住。"
        f"这类卡住多半是在等一把不会释放的锁（或浏览器驱动不再响应），"
        f"任务线程无法被安全中断，建议重启后端回收资源后重试。"
    )


def _cancelled_message() -> str:
    """中断的结论说明。

    刻意把"结论已经定死"写出来：用户最怕的是"我点了取消，它到底还发不发？"
    —— 底层那个线程可能真的还卡在浏览器里，所以必须讲清楚"它再也改不了这条结论"，
    以及"资源可能还没回收干净"。
    """
    return (
        "已手动中断。这条结论已经定死——底层线程即使后来才从浏览器里返回，"
        "也不会把这条任务改写成「成功」，请到平台确认是否已经发出去。"
        "若它卡在浏览器里出不来，这个账号可能还被占着，彻底回收资源需重启后端。"
    )


def _cancel_task(task_id: str) -> tuple[bool, str, str]:
    """中断一个任务。返回 `(是否成功, 给用户看的话, 中断前的状态)`。

    分两种情形，但**都必须立刻给结论**：

    * **还没开始**（排队中）——置上标志就够了，队列里的 worker 取到它时会直接跳过；
    * **正在跑**——先置协作标志、再往它自己的事件循环里塞一个 `Task.cancel()`，
      能真的把 `CancelledError` 抛进正在 await 的协程里。

    关键在最后一步：**不能等线程自己收尾**。它可能永远不返回（等一个不再响应的
    驱动是老问题），等它就等于没中断——用户还是盯着"发布中"转圈。所以这里复用
    看门狗那套：结论定死（`final=True`，迟到的成功回调改不了它）、
    通道立刻放行去跑下一个任务。
    """
    with _lock:
        task = _tasks.get(task_id)
        if task is None:
            return False, "任务不存在（后端可能已重启）", ""
        status = task["status"]
        if status in FINISHED_STATUSES:
            # 已经取消了不报错：用户点的中断很可能就是它变成取消的原因（竞态窗口），
            # 这时候回一句"不用中断了"只会让人以为没生效。
            if status == STATUS_CANCELLED:
                return True, "任务已经是「已取消」", status
            return False, f"任务已经是「{STATUS_TEXT.get(status, status)}」，不用中断", status
        cancel_event = _task_cancel_event(task)
        concluded = task.get("_concluded")

    cancel_event.set()
    _interrupt_async_run(task_id)
    if concluded is not None:
        # 让通道立刻脱身去处理下一个任务，而不是干等这个（可能永远不返回的）线程。
        concluded.set()

    was_running = status == STATUS_RUNNING
    _finish_task(task_id, STATUS_CANCELLED, _cancelled_message(), final=was_running)
    return True, "已中断" if was_running else "已取消（还没开始）", status


def _arm_watchdog(
    task_id: str, concluded: threading.Event | None = None
) -> threading.Timer:
    """给一个任务装上超时看门狗；正常结束时必须用 `_disarm_watchdog` 取消。

    计时从任务**真正开始执行**算起（在 `_run_task` 里装），不含通道排队时间——
    排队等前一个任务跑完是正常的，不该被算作卡住。

    `concluded` 由通道线程传进来：看门狗一旦判定卡住就把它 set 上，通道立刻就能
    放弃这个卡死的线程去处理下一个任务。**不能靠"等一个固定宽限期"**——
    实测那样会让通道白等十几分钟，排在后面的任务一直停在「排队中」。
    """

    def _on_timeout() -> None:
        try:
            with _lock:
                task = _tasks.get(task_id)
                if task is None or task.get("status") != STATUS_RUNNING:
                    return  # 已经正常收尾，或压根还没开始跑
            print(f"⏱️ 发布任务执行超时，判定卡住: {task_id}")
            _finish_task(task_id, STATUS_FAILED, _watchdog_message(), final=True)
        finally:
            # 无论判成什么，都要放通道走——否则它只能干等到自己的兜底超时
            if concluded is not None:
                concluded.set()

    timer = threading.Timer(PUBLISH_TASK_TIMEOUT, _on_timeout)
    timer.daemon = True
    timer.name = f"publish-watchdog-{task_id[:8]}"
    timer.start()
    return timer


def _disarm_watchdog(timer: threading.Timer | None) -> None:
    if timer is not None:
        timer.cancel()


def _run_task(task_id: str, watchdog_signal: threading.Event | None = None) -> None:
    with _lock:
        task = _tasks.get(task_id)
    if task is None:
        return

    cancel_event = _task_cancel_event(task)
    # 把通道的「可以往下走了」信号挂到任务上：用户点中断时要让通道**立刻**放行，
    # 不能等这个可能永远不返回的线程（看门狗也是靠同一个信号脱身的）。
    task["_concluded"] = watchdog_signal

    if task["status"] == STATUS_CANCELLED or cancel_event.is_set():
        # 排队期间就被取消了：一次浏览器都别开，直接按取消收尾。
        _finish_task(task_id, STATUS_CANCELLED, _cancelled_message())
        return

    started = time.time()
    # started_at 在内存里必须是时间戳（要算耗时），落库要的是本地时间字符串，
    # 所以这里分开写：先更新内存状态，再单独把 started_at 写进记录。
    with _lock:
        if task_id in _tasks:
            _tasks[task_id]["started_at"] = started
    _update_task(
        task_id,
        status=STATUS_RUNNING,
        stage=progress.STAGE_PREPARING,
        stage_text=progress.STAGE_TEXT[progress.STAGE_PREPARING],
        action="准备任务",
        message="",
    )
    _update_record(task["record_id"], started_at=_now_text(started))

    progress.bind(lambda event: _task_event(task_id, event), PLATFORMS[int(task["platform"])]["business"])
    prompts.bind(lambda event: _task_prompt(task_id, event))
    watchdog: threading.Timer | None = None
    try:
        # 看门狗从这里起算（真正开始执行），不含通道排队时间。
        # 装在 try 里面而不是外面：万一装表本身就失败（比如线程资源耗尽），异常会被
        # 下面的 except 接住把任务判失败；装在外面的话这条通道会直接死掉。
        watchdog = _arm_watchdog(task_id, watchdog_signal)
        progress.report(progress.STAGE_PREPARING, "检查素材与账号")
        _validate_task(task)

        # 账号互斥（同账号不能同时开两个浏览器）+ 整机并发上限都在 browser_session 里；
        # 需要排队时会回调 on_wait，把"在等什么"写到任务上，界面上就不会看着像卡死。
        # cancel_event 传进去之后，排队期间点中断能立刻退出，不用白等满 10 分钟。
        account_path = str(Path(BASE_DIR) / "cookiesFile" / task["account_file"])
        with browser_session(
            account_path,
            on_wait=lambda text: progress.report(progress.STAGE_LAUNCHING, text),
            cancel_event=cancel_event,
        ):
            progress.report(progress.STAGE_LAUNCHING, "启动浏览器")
            _invoke_uploader(task)

        steps = _json_safe(task.get("steps") or [])
        degraded = [s for s in steps if s.get("status") == "skipped"]
        _finish_task(task_id, STATUS_SUCCESS, _success_message(task, steps, degraded))
    except BrowserSessionCancelled as exc:
        # 还在排队（等账号锁 / 等并发名额）时被中断：浏览器一次都没开，
        # 锁和名额都已释放，这是最干净的一种取消。
        _finish_task(task_id, STATUS_CANCELLED, f"已取消：{exc}")
    except PublishCancelled:
        # 异步中断打断了一个真正在跑的发布，收尾情况取决于上传器当时走到哪一步。
        _finish_task(task_id, STATUS_CANCELLED, _cancelled_message())
    except Exception as exc:
        # 注意：如果用户已经点过中断，这里的结论会被 `_stuck` 挡住（见 _finish_task）——
        # 中断时浏览器关不干净抛的错不该把「已取消」改写成「失败」。
        reason = str(exc) or exc.__class__.__name__
        steps = _json_safe(task.get("steps") or [])
        if steps:
            _update_task(task_id, steps=steps)
        _finish_task(task_id, STATUS_FAILED, reason)
    finally:
        _disarm_watchdog(watchdog)
        progress.unbind()
        prompts.unbind()
        with _lock:
            if task_id in _tasks:
                _tasks[task_id]["_concluded"] = None


def _success_message(task: dict, steps: list, degraded: list) -> str:
    """完成后的说明。措辞按"证据强度"来，不把"没报错"说成"已发布"。"""
    if task["schedule_at"]:
        message = f"已定时提交（{task['schedule_at']}）"
    elif task["platform"] == 2 and task["options"].get("isDraft"):
        message = "已保存为草稿"
    elif any(s.get("step") == "verify_published" and s.get("status") == "ok" for s in steps):
        # 小红书的发布后校验：在笔记列表里真的看到了这条笔记
        message = "已发布成功"
    else:
        message = "已提交，建议到平台确认发布结果"
    if degraded:
        names = "、".join(str(s.get("step", "")) for s in degraded)
        message += f"；{len(degraded)} 个增强步骤降级（{names}）"
    return message


def _worker_loop(channel: str) -> None:
    """一条通道一个执行线程：通道内串行，通道之间并行。

    为什么通道内要串行：同一个账号不能同时开两个浏览器（会互相踢下线）。
    为什么通道之间能并行：不同平台 / 不同账号用的是不同 cookie、不同浏览器，互不相干。

    任务实际跑在**子线程**里，而且只等 `PUBLISH_TASK_TIMEOUT` 多一点：卡死的任务线程
    收不回来（Python 杀不掉线程），但不能让它把整条通道永久堵死——否则同一通道里排在
    后面的任务会一直停在「排队中」，用户看到的是又一次"卡着"。超时后放弃那个线程，
    继续处理下一个任务（它的结论已由看门狗写成失败）。
    """
    task_queue = _queues[channel]
    while True:
        task_id = task_queue.get()
        try:
            # 看门狗判定卡住时会 set 这个事件，通道立刻就能往下走。
            # 兜底再加一段余量：万一看门狗自己也没能触发（极端情况），别让通道永远卡住。
            concluded = threading.Event()
            runner = threading.Thread(
                target=_run_task,
                args=(task_id, concluded),
                daemon=True,
                name=f"publish-task-{task_id[:8]}",
            )
            runner.start()
            backstop = time.time() + PUBLISH_TASK_TIMEOUT + 120
            while runner.is_alive():
                if concluded.wait(0.25):
                    break          # 看门狗已判卡死，别再等这个线程
                if time.time() > backstop:
                    break
            if runner.is_alive():
                print(
                    f"⚠️ 通道 {channel} 的任务线程未能结束，已放弃它并继续下一个任务: {task_id}"
                )
        except Exception as exc:  # 执行器自身出错也不能让线程死掉，否则这条通道就废了
            print(f"⚠️ 发布任务执行器异常（通道 {channel}）: {exc}")
            try:
                _finish_task(task_id, STATUS_FAILED, f"执行器异常: {exc}")
            except Exception:
                pass
        finally:
            task_queue.task_done()


# --------------------------------------------------------------------------- #
# 组装批次
# --------------------------------------------------------------------------- #


def _parse_daily_hours(daily_times) -> list[int]:
    """把前端传的时间点归一化成小时整数列表。

    上游 `generate_schedule_time_next_day` 把 `daily_times` 当**小时整数**用
    （`hours=hour`），而前端传的是 `'10:00'` 这类字符串——直接透传会 TypeError。
    顺手修掉：接受 `'10:00'` / `'10'` / `10`，并去重排序。
    """
    hours: list[int] = []
    for raw in daily_times or []:
        text = str(raw).strip()
        if not text:
            continue
        head = text.split(":")[0]
        try:
            hour = int(head)
        except ValueError:
            raise ValueError(f"时间点格式不对: {raw}") from None
        if not 0 <= hour <= 23:
            raise ValueError(f"时间点超出范围: {raw}")
        if hour not in hours:
            hours.append(hour)
    return sorted(hours)


def _load_accounts() -> dict[int, dict]:
    with records.db() as conn:
        return {int(row["id"]): dict(row) for row in conn.execute("SELECT * FROM user_info")}


def _material_titles(names: list[str]) -> dict[str, str]:
    """磁盘文件名 → 素材展示名（素材管理里改过名的话，用改后的名字）。"""
    if not names:
        return {}
    with records.db() as conn:
        rows = conn.execute(
            f"SELECT file_path, filename FROM file_records WHERE file_path IN ({','.join('?' * len(names))})",
            names,
        ).fetchall()
    return {row["file_path"]: row["filename"] for row in rows}


def _flatten_targets(targets) -> list[dict]:
    """把请求里的 `{platform, account_ids[]}` 摊平成 `{platform, account_id, account_name}`。

    账号名要顺手查出来：发布记录里老的小红书工作台记录只留了账号名、没有 id（见
    `find_repeat_publishes` 的说明），拿名字一起匹配才不会漏。
    """
    accounts = _load_accounts()
    probes: list[dict] = []
    for target in targets or []:
        if not isinstance(target, dict):
            continue
        try:
            platform = int(target.get("platform") or 0)
        except (TypeError, ValueError):
            continue
        if platform not in PLATFORMS:
            continue
        for raw_id in target.get("account_ids") or []:
            try:
                account_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            account = accounts.get(account_id) or {}
            probes.append(
                {
                    "platform": platform,
                    "account_id": account_id,
                    "account_name": account.get("userName") or "",
                }
            )
    return probes


def _repeat_publish_log_notes(drafts: list[dict], confirmed: bool) -> dict[tuple, str]:
    """挑出「这个账号已经发过这条素材」的任务，返回 {(平台, 账号id, 素材): 日志文案}。

    提交时再查一次——前端那次确认到真正提交可能已经隔了一会儿，接口也不该假设调用方
    一定查过（命令行、脚本都会直接打这个接口）。这里**不拦**，只把结论写进任务日志：
    事后翻发布记录，"知情重发"和"系统没提醒就发重了"能分得清。
    """
    probes: list[dict] = []
    seen: set[tuple] = set()
    for draft in drafts:
        key = (int(draft["platform"]), int(draft["account_id"]))
        if key in seen:
            continue
        seen.add(key)
        probes.append(
            {
                "platform": key[0],
                "account_id": key[1],
                "account_name": draft["account_name"],
            }
        )
    conflicts = records.find_repeat_publishes(probes, [d["material"] for d in drafts])
    notes: dict[tuple, str] = {}
    for item in conflicts:
        note = f"重复发布：这个账号{item['message']}"
        note += "，用户已确认重发" if confirmed else "（提交时没有经过二次确认）"
        notes[(item["platform"], item["account_id"], item["material"])] = note
    return notes


def _build_tasks(body: dict) -> tuple[dict, list[dict]]:
    items = body.get("items") or []
    targets = body.get("targets") or []
    raw_options = body.get("options") or {}

    if not isinstance(items, list) or not items:
        raise ValueError("请先选择素材")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"一次最多发布 {MAX_ITEMS} 条素材")
    if not isinstance(targets, list) or not targets:
        raise ValueError("请至少勾选一个平台并选择账号")

    options = {
        "isOriginal": bool(raw_options.get("isOriginal")),
        "isDraft": bool(raw_options.get("isDraft")),
        "enableTimer": bool(raw_options.get("enableTimer")),
        "videosPerDay": int(raw_options.get("videosPerDay") or 1),
        "dailyTimes": raw_options.get("dailyTimes") or [],
        "startDays": int(raw_options.get("startDays") or 0),
        "productLink": (raw_options.get("productLink") or "").strip(),
        "productTitle": (raw_options.get("productTitle") or "").strip(),
        "thumbnail": (raw_options.get("thumbnail") or "").strip(),
        # 用户是否已经为"同账号发过的素材"点过确认（前端弹窗确认后带上）。
        # 只影响任务日志的措辞，不影响发不发——见 /check-published 的说明。
        "confirmRepublish": bool(raw_options.get("confirmRepublish")),
    }

    accounts = _load_accounts()
    normalized_items: list[dict] = []
    for item in items:
        material = (item.get("material") or "").strip()
        title = (item.get("title") or "").strip()
        if not material:
            raise ValueError("素材名不能为空")
        if not title:
            raise ValueError(f"素材「{material}」还没有标题，发布前请先补上")
        tags = [str(t).strip().lstrip("#") for t in (item.get("tags") or []) if str(t).strip()]
        normalized_items.append(
            {
                "material": material,
                "title": title,
                "desc": (item.get("desc") or "").strip(),
                "tags": tags,
            }
        )

    normalized_targets: list[dict] = []
    for target in targets:
        platform = int(target.get("platform") or 0)
        if platform not in PLATFORMS:
            raise ValueError(f"不支持的平台: {target.get('platform')}")
        account_ids = [int(a) for a in (target.get("account_ids") or [])]
        if not account_ids:
            raise ValueError(f"{PLATFORMS[platform]['name']} 还没有选择账号")
        for account_id in account_ids:
            if account_id not in accounts:
                raise ValueError(f"账号不存在（可能已被删除）: id={account_id}")
            # 账号是分平台的（user_info.type）。前端只会列对应平台的账号，
            # 但接口是公开的，这里再挡一道——不然会拿抖音的 cookie 去发小红书，
            # 报错信息会指向莫名其妙的地方。
            account_type = int(accounts[account_id].get("type") or 0)
            if account_type != platform:
                raise ValueError(
                    f"账号「{accounts[account_id].get('userName')}」属于{PLATFORMS.get(account_type, {}).get('name', '未知平台')}"
                    f"，不能发到{PLATFORMS[platform]['name']}"
                )
        normalized_targets.append({"platform": platform, "account_ids": account_ids})

    total = len(normalized_items) * sum(len(t["account_ids"]) for t in normalized_targets)
    if total > MAX_TASKS_PER_JOB:
        raise ValueError(f"一次最多 {MAX_TASKS_PER_JOB} 个发布任务，当前是 {total} 个，请分批发")

    # 定时发布：同一(平台,账号)下的素材按顺序分配到不同天。
    # 调 `generate_schedule_time_next_day` 必须用关键字传参——上游几个 post_video_*
    # 是位置传参，把 start_days 传进了 timestamps，是个隐藏 bug，这里不跟着踩。
    schedules: dict[tuple[int, int], list] = {}
    if options["enableTimer"]:
        hours = _parse_daily_hours(options["dailyTimes"])
        if not hours:
            raise ValueError("定时发布至少要选一个时间点")
        videos_per_day = options["videosPerDay"]
        if videos_per_day <= 0:
            raise ValueError("每天发布条数必须大于 0")
        if videos_per_day > len(hours):
            raise ValueError(f"每天发 {videos_per_day} 条，但只选了 {len(hours)} 个时间点，请补齐时间点")
        for target in normalized_targets:
            for account_id in target["account_ids"]:
                schedules[(target["platform"], account_id)] = generate_schedule_time_next_day(
                    len(normalized_items),
                    videos_per_day=videos_per_day,
                    daily_times=hours,
                    start_days=options["startDays"],
                )

    material_names = [item["material"] for item in normalized_items]
    titles = _material_titles(material_names)

    tasks: list[dict] = []
    for index, item in enumerate(normalized_items):
        for target in normalized_targets:
            platform = target["platform"]
            for account_id in target["account_ids"]:
                account = accounts[account_id]
                publish_at = 0
                if options["enableTimer"]:
                    publish_at = schedules[(platform, account_id)][index]
                tasks.append(
                    {
                        "platform": platform,
                        "platform_name": PLATFORMS[platform]["name"],
                        "account_id": account_id,
                        "account_name": account.get("userName") or str(account_id),
                        "account_file": account.get("filePath") or "",
                        "material": item["material"],
                        "material_title": titles.get(item["material"]) or item["material"],
                        "title": item["title"],
                        "desc": item["desc"],
                        "tags": item["tags"],
                        "publish_at": publish_at,
                        "schedule_at": publish_at.strftime("%Y-%m-%d %H:%M") if publish_at else "",
                        "options": options,
                        # 发布途中"要人搭把手"的输入请求（抖音短信验证码等），
                        # 由 myUtils/publish_prompt.py 在需要时挂上，见 _task_prompt。
                        "prompt": None,
                    }
                )
    return options, tasks


# --------------------------------------------------------------------------- #
# 接口：提交 / 进度 / 取消
# --------------------------------------------------------------------------- #


@publish_bp.post("/check-published")
def publish_check_published():
    """这批「素材 × 平台 × 账号」里，哪些此前已经发过了？（给前端做二次确认）

    只管**查和报**，不下结论也不拦人：重发有时就是用户想要的，界面上问一句
    "这些这个账号已经发过了，还要发吗"就够了。

    解析一律宽松——素材名缺失、账号被删、平台不认识都只是**跳过**那些组合，不报错：
    这个接口是提醒人的，不该因为一个脏参数把发布本身挡下来（发布该报的错由
    `/publish/submit` 去报）。
    """
    body = request.get_json(silent=True) or {}
    items = body.get("items") or []
    if not isinstance(items, list):
        items = []

    materials: list[str] = []
    for item in items:
        name = str((item or {}).get("material") or "").strip() if isinstance(item, dict) else ""
        if name and name not in materials:
            materials.append(name)

    probes = _flatten_targets(body.get("targets"))
    conflicts = records.find_repeat_publishes(probes, materials)
    for conflict in conflicts:
        platform = PLATFORMS.get(conflict["platform"], {})
        conflict["platform_name"] = conflict.get("platform_name") or platform.get("name") or ""
        if not conflict.get("account_name"):
            conflict["account_name"] = (
                f"账号{conflict['account_id']}" if conflict.get("account_id") else "未知账号"
            )

    return jsonify(
        {
            "code": 200,
            "msg": "ok",
            "data": {
                "conflicts": conflicts,
                "total": len(conflicts),
                "published": sum(1 for c in conflicts if c["published"]),
                "in_flight": sum(1 for c in conflicts if c["in_flight"]),
                # 这一批一共要发多少条（素材 × 账号），界面上好说"N 条里有 M 条发过"
                "task_count": len(materials) * len(probes),
            },
        }
    )


@publish_bp.post("/submit")
def publish_submit():
    body = request.get_json(silent=True) or {}
    try:
        options, drafts = _build_tasks(body)
    except ValueError as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400

    job_id = uuid.uuid4().hex
    created = time.time()
    task_ids: list[str] = []
    records: list[dict] = []

    # 同账号发过的素材（可能刚在前端确认过），给这些任务在日志里留一句。
    # 查的是提交这一刻的发布记录，跟着 `options.confirmRepublish` 一起决定措辞。
    repeat_notes = _repeat_publish_log_notes(
        drafts, bool(options.get("confirmRepublish"))
    )

    # 先把整批任务备齐，注册批次，**最后**才入队。
    # 顺序不能反：worker 一旦取到任务就可能秒失败并回调 `_refresh_job`，
    # 那时如果批次还没注册，这一批就永远停在"running"了。
    for draft in drafts:
        task_id = uuid.uuid4().hex
        note = repeat_notes.get((int(draft["platform"]), int(draft["account_id"]), draft["material"]))
        task = dict(
            draft,
            task_id=task_id,
            job_id=job_id,
            created_at=created,
            status=STATUS_QUEUED,
            stage=STATUS_QUEUED,
            stage_text=STATUS_TEXT[STATUS_QUEUED],
            action="已加入发布队列",
            message="",
            started_at=None,
            finished_at=None,
            duration=None,
            log=([{"t": _now_text(created), "text": note, "level": "WARNING"}] if note else []),
            log_count=1 if note else 0,
            steps=[],
            queue_position=0,
            # 中断标志：提交那一刻就建好，别等真要中断时才发现没地方挂。
            # 必须用下划线开头——`_public_task` 靠这个前缀把内部字段挡在 JSON 之外，
            # 否则 `threading.Event` 会被 json_safe 转成 "<threading.Event object …>"
            # 直接漏给前端。
            _cancel_event=threading.Event(),
        )
        task["record_id"] = _insert_record(task)
        task["channel"] = _channel_key(task)
        with _lock:
            _tasks[task_id] = task
            _waiting.setdefault(task["channel"], []).append(task_id)
        task_ids.append(task_id)
        records.append(
            {
                "task_id": task_id,
                "record_id": task["record_id"],
                "platform": task["platform"],
                "platform_name": task["platform_name"],
                "account_name": task["account_name"],
                "material": task["material"],
                "material_title": task["material_title"],
                "title": task["title"],
            }
        )

    with _lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "created_at": created,
            "status": "running",
            "task_ids": task_ids,
            "options": options,
        }

    # 每条通道各自入队：不同平台 / 不同账号的线程互不等待，同一账号的才排队。
    for task_id in task_ids:
        with _lock:
            task = _tasks.get(task_id)
        if task is not None:
            _ensure_worker(task["channel"])
            _queues[task["channel"]].put(task_id)

    return jsonify(
        {
            "code": 200,
            "msg": f"已提交 {len(task_ids)} 个发布任务",
            "data": {"job_id": job_id, "task_count": len(task_ids), "tasks": records},
        }
    )


def _waiting_positions() -> dict[str, int]:
    """每个任务在**自己那条通道**里排第几。不同通道各排各的，不互相算。"""
    with _lock:
        positions: dict[str, int] = {}
        for queue_list in _waiting.values():
            for index, task_id in enumerate(queue_list):
                positions[task_id] = index + 1
        return positions


def _public_task(task: dict, positions: dict[str, int], log_tail: int = 3) -> dict:
    data = {k: v for k, v in task.items() if not k.startswith("_")}
    data["steps"] = _live_steps(task)
    data["status_text"] = STATUS_TEXT.get(task["status"], task["status"])
    data["queue_position"] = positions.get(task["task_id"], 0) if task["status"] == STATUS_QUEUED else 0
    # 让前端知道这条该不该显示「中断」按钮，而不是自己再抄一份状态判断
    data["cancellable"] = task["status"] in (STATUS_QUEUED, STATUS_RUNNING)
    logs = task["log"]
    data["log"] = logs[-log_tail:] if log_tail else []
    data["log_count"] = task.get("log_count", len(logs))
    return _json_safe(data)


def _summary(tasks: list[dict]) -> dict:
    summary = {
        "total": len(tasks),
        "queued": 0,
        "running": 0,
        "success": 0,
        "failed": 0,
        "cancelled": 0,
        "interrupted": 0,
        "done": 0,
    }
    for task in tasks:
        status = task.get("status", STATUS_QUEUED)
        summary[status] = summary.get(status, 0) + 1
        if status in FINISHED_STATUSES:
            summary["done"] += 1
    summary["percent"] = round(summary["done"] * 100 / summary["total"]) if summary["total"] else 0
    return summary


@publish_bp.get("/job/<job_id>")
def publish_job(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({"code": 404, "msg": "批次不存在（后端可能已重启）", "data": None}), 404
        tasks = [_tasks[tid] for tid in job["task_ids"] if tid in _tasks]
        job_state = job["status"]
    positions = _waiting_positions()
    public = [_public_task(task, positions) for task in tasks]
    return jsonify(
        {
            "code": 200,
            "msg": "ok",
            "data": {
                "job_id": job_id,
                "status": job_state,
                "created_at": job["created_at"],
                "summary": _summary(tasks),
                "tasks": public,
                # 让界面上能说清"为什么只并行跑了这么几个"
                "max_concurrent_browsers": MAX_CONCURRENT_BROWSERS,
                "channels": sorted({task.get("channel") or _channel_key(task) for task in tasks}),
            },
        }
    )


@publish_bp.get("/task/<task_id>")
def publish_task(task_id: str):
    with _lock:
        task = _tasks.get(task_id)
    if task is None:
        return jsonify({"code": 404, "msg": "任务不存在（后端可能已重启）", "data": None}), 404
    data = _public_task(task, _waiting_positions(), log_tail=0)
    data["log"] = _json_safe(task["log"])
    return jsonify({"code": 200, "msg": "ok", "data": data})


@publish_bp.post("/task/<task_id>/cancel")
def publish_task_cancel(task_id: str):
    """中断**单个**任务：排队中的、正在跑的都能中断。

    为什么要有这个接口：一个账号的通道是串行的，某条任务卡住时，用户想保住的往往
    是"后面那几条"而不是"整批"，所以取消粒度必须细到单个任务。
    """
    ok, message, previous = _cancel_task(task_id)
    if not ok:
        code = 404 if not previous and "不存在" in message else 400
        return jsonify({"code": code, "msg": message, "data": None}), code
    return jsonify(
        {
            "code": 200,
            "msg": message,
            "data": {"task_id": task_id, "previous_status": previous},
        }
    )


@publish_bp.get("/prompts")
def publish_prompts():
    """当前所有"发布途中在等人输入"的请求（抖音短信验证码…）。

    刻意做成**与批次无关的全局接口**：验证码等的是"坐在电脑前的这个人"，
    跟用户此刻停在哪个页面没关系。前端在布局层（App.vue）轮询它，
    不管在看发布记录还是模型设置，该弹的窗都能弹出来。
    """
    with _lock:
        items = [
            _public_prompt(task)
            for task in _tasks.values()
            if task["status"] == STATUS_RUNNING
        ]
    items = [item for item in items if item]
    items.sort(key=lambda item: item.get("created_at") or 0)
    return jsonify({"code": 200, "msg": "ok", "data": items})


@publish_bp.post("/prompts/<prompt_id>")
def publish_prompt_submit(prompt_id: str):
    """回答一次"等人输入"。body：`{"value": "123456"}` 或 `{"skip": true}`。"""
    body = request.get_json(silent=True) or {}
    if body.get("skip"):
        if prompts.skip(prompt_id):
            return jsonify({"code": 200, "msg": "已跳过这次输入", "data": None})
        return jsonify({"code": 404, "msg": "这次输入已经不需要了（超时或已填写）", "data": None}), 404

    value = str(body.get("value") or "").strip()
    if not value:
        return jsonify({"code": 400, "msg": "请填写内容后再提交", "data": None}), 400
    if prompts.answer(prompt_id, value):
        return jsonify({"code": 200, "msg": "已提交", "data": None})
    return jsonify({"code": 404, "msg": "这次输入已经不需要了（超时或已填写）", "data": None}), 404


@publish_bp.get("/jobs")
def publish_jobs():
    """最近的批次。`?active=1` 只看还没跑完的——前端刷新页面后据此恢复进度视图。"""
    active_only = request.args.get("active") in ("1", "true", "yes")
    with _lock:
        jobs = [dict(job) for job in _jobs.values()]
    result = []
    for job in jobs:
        with _lock:
            tasks = [_tasks[tid] for tid in job["task_ids"] if tid in _tasks]
        if active_only and job["status"] == "done":
            continue
        result.append(
            {
                "job_id": job["job_id"],
                "created_at": job["created_at"],
                "status": job["status"],
                "summary": _summary(tasks),
            }
        )
    result.sort(key=lambda item: item["created_at"], reverse=True)
    return jsonify({"code": 200, "msg": "ok", "data": result[:20]})


@publish_bp.post("/job/<job_id>/cancel")
def publish_job_cancel(job_id: str):
    """取消一个批次里还没结束的任务。

    - 默认只取消**还没开始**的（界面上的「取消未开始的任务」）；
    - 带 `running=1`（或 body `{"running": true}`）时，**正在跑的也一起中断**。

    为什么默认不中断正在跑的：中断一个真在发一半的任务是有代价的——它可能已经把
    内容提交到平台了，只是还没走完流程。所以这件事得用户明确要（前端会二次确认）。
    """
    body = request.get_json(silent=True) or {}
    also_running = (
        request.args.get("running") in ("1", "true", "yes")
        or body.get("running") is True
    )
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({"code": 404, "msg": "批次不存在", "data": None}), 404
        task_ids = [tid for tid in job["task_ids"] if tid in _tasks]

    cancelled: list[str] = []
    interrupted = 0
    for task_id in task_ids:
        with _lock:
            task = _tasks.get(task_id)
            status = task["status"] if task else None
        if status is None:
            continue
        if status == STATUS_QUEUED or (also_running and status == STATUS_RUNNING):
            ok, _msg, previous = _cancel_task(task_id)
            if ok:
                cancelled.append(task_id)
                if previous == STATUS_RUNNING:
                    interrupted += 1

    if not cancelled:
        return jsonify(
            {"code": 200, "msg": "没有可取消的任务（都已经结束了）", "data": {"cancelled": 0, "interrupted": 0}}
        )
    if also_running:
        message = f"已取消 {len(cancelled)} 个任务"
        message += f"（含 {interrupted} 个正在跑的）" if interrupted else "（都是还没开始的）"
    else:
        message = f"已取消 {len(cancelled)} 个未开始的任务"
    return jsonify(
        {
            "code": 200,
            "msg": message,
            "data": {"cancelled": len(cancelled), "interrupted": interrupted},
        }
    )


# --------------------------------------------------------------------------- #
# 接口：发布记录
# --------------------------------------------------------------------------- #


@publish_bp.get("/records")
def publish_records():
    """发布记录列表。

    支持筛选：来源 / 平台 / 账号 / 状态 / 关键字（标题或素材）/ 时间范围。
    一起返回当前筛选下的**状态统计**，页头可以直接显示"成功 N / 失败 M"。
    """
    try:
        page = max(int(request.args.get("page") or 1), 1)
        page_size = min(max(int(request.args.get("page_size") or 20), 1), 200)
    except ValueError:
        page, page_size = 1, 20

    where: list[str] = []
    params: list = []
    source = (request.args.get("source") or "").strip()
    if source:
        if source == records.SOURCE_PUBLISH_CENTER:
            # `source` 是后加的列，之前发的记录是 NULL——那会儿只有发布中心，
            # 所以 NULL 一律按「发布中心」算，不然筛"发布中心"会漏掉这些历史记录。
            where.append("(source = ? OR source IS NULL OR source = '')")
            params.append(source)
        else:
            where.append("source = ?")
            params.append(source)
    platform = (request.args.get("platform") or "").strip()
    if platform:
        where.append("platform = ?")
        params.append(int(platform) if platform.isdigit() else -1)
    account = (request.args.get("account") or "").strip()
    if account:
        where.append("account_name = ?")
        params.append(account)
    status = (request.args.get("status") or "").strip()
    if status:
        where.append("status = ?")
        params.append(status)
    keyword = (request.args.get("keyword") or "").strip()
    if keyword:
        where.append("(title LIKE ? OR material LIKE ? OR material_title LIKE ? OR account_name LIKE ?)")
        params.extend([f"%{keyword}%"] * 4)
    start = (request.args.get("start") or "").strip()
    if start:
        where.append("created_at >= ?")
        params.append(f"{start} 00:00:00" if len(start) == 10 else start)
    end = (request.args.get("end") or "").strip()
    if end:
        where.append("created_at <= ?")
        params.append(f"{end} 23:59:59" if len(end) == 10 else end)

    clause = f" WHERE {' AND '.join(where)}" if where else ""
    with records.db() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM publish_records{clause}", params).fetchone()["n"]
        rows = conn.execute(
            f"""
            SELECT {records.select_columns()}
              FROM publish_records{clause}
             ORDER BY id DESC
             LIMIT ? OFFSET ?
            """,
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        stat_rows = conn.execute(
            f"SELECT status, COUNT(*) AS n FROM publish_records{clause} GROUP BY status", params
        ).fetchall()
        # 筛选项的下拉候选：给全部记录（不带当前筛选），否则选了平台就看不到别的平台账号了
        account_rows = conn.execute(
            "SELECT DISTINCT account_name, platform FROM publish_records ORDER BY platform, account_name"
        ).fetchall()
        source_rows = conn.execute(
            """
            SELECT DISTINCT COALESCE(NULLIF(source, ''), ?) AS source
              FROM publish_records
            """,
            (records.SOURCE_PUBLISH_CENTER,),
        ).fetchall()

    stats = {row["status"]: row["n"] for row in stat_rows}
    return jsonify(
        {
            "code": 200,
            "msg": "ok",
            "data": {
                "items": [records.record_row(row) for row in rows],
                "total": total,
                "page": page,
                "page_size": page_size,
                "stats": stats,
                "accounts": [
                    {"name": row["account_name"], "platform": row["platform"]} for row in account_rows
                ],
                "sources": [
                    {"value": row["source"], "label": records.SOURCE_TEXT.get(row["source"], row["source"])}
                    for row in source_rows
                ],
            },
        }
    )


@publish_bp.get("/records/<int:record_id>")
def publish_record_detail(record_id: int):
    with records.db() as conn:
        row = conn.execute("SELECT * FROM publish_records WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        return jsonify({"code": 404, "msg": "记录不存在", "data": None}), 404
    return jsonify({"code": 200, "msg": "ok", "data": records.record_row(row)})
