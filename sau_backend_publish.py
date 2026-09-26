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
POST /publish/job/<job_id>/cancel 取消批次里还没开始的任务
GET  /publish/records             发布记录（分页 + 筛选 + 统计）
GET  /publish/records/<record_id> 单条记录详情
```

已知限制
--------
任务没有超时上限：上游上传器里有若干无上限的 `while True` 等待（等页面跳转、
等上传完成），一旦卡住会一直占着执行线程，后面的任务只能排队。出现这种情况
重启后端即可——重启会把残留的 running/queued 标成「已中断」，记录不会说谎。
"""

from __future__ import annotations

import asyncio
import json
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
from myUtils.browser_lock import MAX_CONCURRENT_BROWSERS, browser_session
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
}

# (立即发布, 定时发布) 两个策略常量。每个平台都得显式传：
# 上传器的 `validate_base_args` 在策略为 immediate 时会把 publish_date 清零，
# 默认值又都是 immediate——定时发布如果不显式给 scheduled，时间会被悄悄丢掉。
PUBLISH_STRATEGY: dict[int, tuple[str, str]] = {
    1: (XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE, XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED),
    2: (TENCENT_PUBLISH_STRATEGY_IMMEDIATE, TENCENT_PUBLISH_STRATEGY_SCHEDULED),
    3: (DOUYIN_PUBLISH_STRATEGY_IMMEDIATE, DOUYIN_PUBLISH_STRATEGY_SCHEDULED),
    4: (KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE, KUAISHOU_PUBLISH_STRATEGY_SCHEDULED),
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


def _finish_task(task_id: str, status: str, message: str) -> None:
    now = time.time()
    with _lock:
        task = _tasks.get(task_id)
        if task is None:
            return
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


def _invoke_uploader(task: dict) -> None:
    """按平台构造上传器并跑完一次发布（一个素材 × 一个账号）。"""
    platform = int(task["platform"])
    file_path = str(Path(BASE_DIR) / "videoFile" / task["material"])
    account_file = str(Path(BASE_DIR) / "cookiesFile" / task["account_file"])
    publish_at = task["publish_at"]  # datetime | 0
    scheduled = publish_at != 0
    strategy = PUBLISH_STRATEGY[platform][1 if scheduled else 0]
    options = task["options"]

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
        asyncio.run(app.main())
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
        asyncio.run(app.main())
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
        asyncio.run(app.douyin_upload_video())
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
        asyncio.run(app.main())
        return

    raise RuntimeError(f"不支持的平台类型: {platform}")


def _attach_app(task_id: str, app) -> None:
    with _lock:
        if task_id in _tasks:
            _tasks[task_id]["_app"] = app


def _run_task(task_id: str) -> None:
    with _lock:
        task = _tasks.get(task_id)
    if task is None:
        return
    if task["status"] == STATUS_CANCELLED:
        _finish_task(task_id, STATUS_CANCELLED, "已取消")
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
    try:
        progress.report(progress.STAGE_PREPARING, "检查素材与账号")
        _validate_task(task)

        # 账号互斥（同账号不能同时开两个浏览器）+ 整机并发上限都在 browser_session 里；
        # 需要排队时会回调 on_wait，把"在等什么"写到任务上，界面上就不会看着像卡死。
        account_path = str(Path(BASE_DIR) / "cookiesFile" / task["account_file"])
        with browser_session(account_path, on_wait=lambda text: progress.report(progress.STAGE_LAUNCHING, text)):
            progress.report(progress.STAGE_LAUNCHING, "启动浏览器")
            _invoke_uploader(task)

        steps = _json_safe(task.get("steps") or [])
        degraded = [s for s in steps if s.get("status") == "skipped"]
        _finish_task(task_id, STATUS_SUCCESS, _success_message(task, steps, degraded))
    except Exception as exc:
        reason = str(exc) or exc.__class__.__name__
        steps = _json_safe(task.get("steps") or [])
        if steps:
            _update_task(task_id, steps=steps)
        _finish_task(task_id, STATUS_FAILED, reason)
    finally:
        progress.unbind()
        prompts.unbind()


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
    """
    task_queue = _queues[channel]
    while True:
        task_id = task_queue.get()
        try:
            _run_task(task_id)
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

    # 先把整批任务备齐，注册批次，**最后**才入队。
    # 顺序不能反：worker 一旦取到任务就可能秒失败并回调 `_refresh_job`，
    # 那时如果批次还没注册，这一批就永远停在"running"了。
    for draft in drafts:
        task_id = uuid.uuid4().hex
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
            log=[],
            log_count=0,
            steps=[],
            queue_position=0,
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
    """取消批次里**还没开始**的任务。已经在跑的那个不受影响（浏览器操作没法安全中断）。"""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({"code": 404, "msg": "批次不存在", "data": None}), 404
        task_ids = [tid for tid in job["task_ids"] if tid in _tasks]
    cancelled = []
    for task_id in task_ids:
        with _lock:
            task = _tasks.get(task_id)
        if task and task["status"] == STATUS_QUEUED:
            _update_task(task_id, status=STATUS_CANCELLED, stage=STATUS_CANCELLED, stage_text=STATUS_TEXT[STATUS_CANCELLED])
            cancelled.append(task_id)
    return jsonify({"code": 200, "msg": f"已取消 {len(cancelled)} 个未开始的任务", "data": {"cancelled": len(cancelled)}})


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
