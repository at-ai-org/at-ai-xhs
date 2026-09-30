# -*- coding: utf-8 -*-
"""小红书 Web 操作接口（at-ai-xhs 改造新增）。

把原本只在命令行上的页面操作能力搬到网页上，全部通过浏览器操作完成：

- `GET  /xhs/accounts`          列出账号（cookie 文件），可选校验有效性
- `GET  /xhs/login/stream`      扫码登录：二维码经 SSE 推到网页，扫码完成即推送结果
- `POST /xhs/selftest`          页面契约自检（不提交任何内容）
- `POST /xhs/collections`       只读列出账号可选合集
- `POST /xhs/publish`           发布视频（支持合集 / 定时 / 可见范围 / 原创 / dry-run）
- `GET  /xhs/task/<task_id>`    发布任务进度（含每一步的状态）

设计约束
--------
**同一个账号只允许一个网页端会话**，并发开两个浏览器会互相踢下线。
因此所有会开浏览器的操作都要抢**同一个账号的锁**（`myUtils/browser_lock.py`）——
宁可排队，也不要互相打断。不同账号之间互不相干，可以并行。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue

from flask import Blueprint, Response, jsonify, request

import myUtils.publish_progress as progress
from conf import BASE_DIR, LOCAL_CHROME_HEADLESS
from myUtils import publish_records as records
from myUtils.browser_lock import browser_session
from myUtils.app_settings import (
    get_default_collection,
    load_settings,
    public_settings,
    save_settings,
)
from uploader.xiaohongshu_uploader.ai_copywriter import chat_completion, generate_note_copy
from uploader.xiaohongshu_uploader.main import (
    XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
    XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED,
    XiaoHongShuVideo,
    cookie_auth as xhs_cookie_auth,
    xiaohongshu_list_collections,
    xiaohongshu_selftest,
    xiaohongshu_setup,
)

xhs_bp = Blueprint("xhs", __name__, url_prefix="/xhs")

# 开浏览器的并发约束都在 myUtils/browser_lock.py：
# **同一个账号**（同一个 cookie 文件）互斥，不同账号可以并行；另外整机有并发上限。
# 发布中心用的是同一套约束，所以"工作台在发账号 A"时"发布中心也发账号 A"会排队。

# 发布任务表：内存态即可，进程重启后任务丢失，前端会提示"任务不存在"
_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()

#: 工作台单个发布任务的执行上限（秒）。
#:
#: 和发布中心 `sau_backend_publish.PUBLISH_TASK_TIMEOUT` **刻意用同一个开关**：
#: 用户在界面上看到的是同一件事（"一条笔记发布中"），不该因为走的是哪个入口而行为不同。
#: 读的是同一个环境变量 SAU_PUBLISH_TASK_TIMEOUT，默认也一样；两个值必须相等，
#: 有测试钉着（tests/test_xhs_task_watchdog.py::TimeoutConfigTest）。
#:
#: 为什么需要：发布是阻塞式浏览器自动化，"卡住"既不抛异常也不超时。工作台这边
#: 每个任务直接起一个线程（没有发布中心那种通道队列），所以卡住时那个线程就永远
#: 收不回来，界面上永远停在「发布中」——现场就发生过。
XHS_TASK_TIMEOUT = float(os.environ.get("SAU_PUBLISH_TASK_TIMEOUT") or 30 * 60)

#: 任务处于这两个状态时，看门狗才有资格判它卡死（其余都是已经落定的终态）
XHS_ACTIVE_STATES = ("queued", "running")


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def _ok(data=None, msg="ok"):
    return jsonify({"code": 200, "success": True, "msg": msg, "data": data})


def _err(msg: str, code: int = 500, data=None):
    return jsonify({"code": code, "success": False, "msg": str(msg), "data": data}), code


def account_file_for(account: str) -> Path:
    """与 CLI 保持一致的账号文件约定：`cookies/xiaohongshu_<account>.json`。

    这样网页上登录的账号，命令行也能直接用，反之亦然。
    """
    safe = "".join(c for c in (account or "default") if c.isalnum() or c in "-_") or "default"
    path = Path(BASE_DIR) / "cookies" / f"xiaohongshu_{safe}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def resolve_video_path(raw: str) -> str:
    """把前端传来的视频标识解析成绝对路径。

    支持两种来源：
    - 素材库文件名（`/upload` 上传后返回的 `uuid_name.mp4`）→ 拼到 `videoFile/`
    - 直接给的绝对路径
    """
    p = Path(raw or "")
    if not p.is_absolute():
        p = Path(BASE_DIR) / "videoFile" / raw
    return str(p)


def _collections_cache_path(account: str) -> Path:
    safe = "".join(c for c in (account or "default") if c.isalnum() or c in "-_") or "default"
    return Path(BASE_DIR) / "cookies" / f"xiaohongshu_{safe}_collections.json"


def _load_collections_cache(account: str) -> dict | None:
    path = _collections_cache_path(account)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_collections_cache(account: str, collections: list[str]) -> None:
    """缓存合集清单。

    有必要缓存的原因：读一次合集要上传视频 + 等转码，是分钟级操作，
    而合集清单平时基本不变。
    """
    try:
        _collections_cache_path(account).write_text(
            json.dumps({"collections": collections, "cached_at": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"写入合集缓存失败（不影响主流程）: {exc}")


def _run_async(coro_factory, timeout: float = 600.0):
    """在独立线程里跑协程并等结果（Flask 同步、patchright 异步）。"""
    box: dict = {}

    def worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            box["result"] = loop.run_until_complete(coro_factory())
        except Exception as exc:  # 交给主线程重抛
            box["error"] = exc
        finally:
            loop.close()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if "error" in box:
        raise box["error"]
    if "result" not in box:
        raise TimeoutError(f"操作超时（>{timeout:.0f}s）")
    return box["result"]


def _task_update(task_id: str, **fields) -> None:
    with _tasks_lock:
        if task_id in _tasks:
            _tasks[task_id].update(fields)
            _tasks[task_id]["updated_at"] = time.time()


def _task_finish(
    task_id: str,
    *,
    state: str,
    message: str,
    steps: list | None = None,
    _from_watchdog: bool = False,
) -> bool:
    """给任务写下**最终结论**，返回是否真的写进去了。

    看门狗判过"卡死"之后，迟到的真实收尾**不许**覆盖这个结论——那是最坏的一种错：
    用户会以为发上去了，实际可能压根没发。道理和发布中心的
    `sau_backend_publish._finish_task` 一样（那边有更详细的说明）。

    返回 False 表示这次收尾被忽略，调用方**连发布记录也别再写**，否则记录和任务会打架。
    """
    with _tasks_lock:
        task = _tasks.get(task_id)
        if task is None:
            return False
        if task.get("_stuck") and not _from_watchdog:
            return False
        fields: dict = {"state": state, "message": message, "finished_at": time.time()}
        if steps is not None:
            fields["steps"] = steps
        if _from_watchdog:
            # 下划线开头：_task_snapshot 会把它过滤掉，不给前端看到
            fields["_stuck"] = True
        task.update(fields)
        task["updated_at"] = time.time()
        return True


def _task_snapshot(task_id: str) -> dict | None:
    with _tasks_lock:
        task = _tasks.get(task_id)
        if task is None:
            return None
        # 逐步进度直接读上传器实例上的 step_results（跑的过程中也能看到）
        app = task.pop("_app", None)
        snapshot = {k: v for k, v in task.items() if not k.startswith("_")}
    if app is not None:
        with _tasks_lock:
            _tasks[task_id]["_app"] = app
        snapshot["steps"] = [r.to_dict() for r in getattr(app, "step_results", [])]
    return snapshot


# --------------------------------------------------------------------------- #
# 账号
# --------------------------------------------------------------------------- #


@xhs_bp.get("/accounts")
def xhs_accounts():
    """列出账号。`?check=1` 时逐个校验 cookie（较慢，每个账号要开一次浏览器）。"""
    do_check = request.args.get("check") in ("1", "true", "yes")
    cookie_dir = Path(BASE_DIR) / "cookies"
    accounts = []
    for f in sorted(cookie_dir.glob("xiaohongshu_*.json")):
        # 排除非 cookie 的旁路文件（例如合集缓存 xiaohongshu_<account>_collections.json），
        # 否则会被当成一个账号列出来。
        if f.name.endswith("_collections.json"):
            continue
        name = f.stem.replace("xiaohongshu_", "", 1)
        item = {
            "account": name,
            "file": str(f),
            "mtime": f.stat().st_mtime,
            "valid": None,
        }
        if do_check:
            try:
                with browser_session(f):
                    item["valid"] = bool(
                        _run_async(lambda p=str(f): xhs_cookie_auth(p), timeout=180)
                    )
            except Exception as exc:
                item["valid"] = False
                item["error"] = str(exc)
        accounts.append(item)
    return _ok(accounts)


@xhs_bp.get("/check")
def xhs_check():
    account = request.args.get("account")
    if not account:
        return _err("缺少 account 参数", 400)
    af = account_file_for(account)
    if not af.exists():
        return _ok({"account": account, "valid": False, "message": "cookie 文件不存在，需要先扫码登录"})
    try:
        with browser_session(af):
            valid = bool(_run_async(lambda p=str(af): xhs_cookie_auth(p), timeout=180))
        return _ok({"account": account, "valid": valid, "message": "cookie 有效" if valid else "cookie 已失效"})
    except Exception as exc:
        return _err(f"校验失败: {exc}")


# --------------------------------------------------------------------------- #
# 扫码登录（SSE）
# --------------------------------------------------------------------------- #


@xhs_bp.get("/login/stream")
def xhs_login_stream():
    """扫码登录。二维码以 data URL 形式通过 SSE 推给网页。

    事件类型：
      - `qrcode` 二维码就绪（data.image_data_url 可直接给 <img :src>）
      - `done`   登录结束（data.success / data.message）
      - `error`  异常
      - `timeout` 长时间没有事件
    """
    account = request.args.get("account") or "default"
    account_file = account_file_for(account)
    queue: Queue = Queue()

    def worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            def on_qrcode(payload: dict):
                queue.put({"event": "qrcode", "account": account, "data": payload})

            with browser_session(account_file):
                result = loop.run_until_complete(
                    xiaohongshu_setup(
                        str(account_file),
                        handle=True,
                        return_detail=True,
                        qrcode_callback=on_qrcode,
                        headless=LOCAL_CHROME_HEADLESS,
                    )
                )
            queue.put({"event": "done", "account": account, "data": result})
        except Exception as exc:
            queue.put({"event": "error", "account": account, "data": str(exc)})
        finally:
            loop.close()

    threading.Thread(target=worker, daemon=True).start()

    def generate():
        yield ": connected\n\n"
        while True:
            try:
                item = queue.get(timeout=300)
            except Empty:
                yield 'data: {"event":"timeout"}\n\n'
                break
            yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
            if item.get("event") in ("done", "error"):
                break

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# --------------------------------------------------------------------------- #
# 只读操作：自检 / 合集列表
# --------------------------------------------------------------------------- #


@xhs_bp.post("/selftest")
def xhs_selftest_api():
    """页面契约自检。

    传 `video` 则做**深自检**：先把测试视频传上去（不会发布），
    把编辑器撑出来后再探测——只有这样才能验证标题/正文/话题/定时/**合集**/发布按钮。
    不传 `video` 则是浅自检，只验证上传区，其余契约必然报缺失（页面本来就没渲染）。
    """
    body = request.get_json(silent=True) or {}
    account = body.get("account")
    if not account:
        return _err("缺少 account", 400)
    af = account_file_for(account)
    if not af.exists():
        return _err("该账号还没有 cookie，请先扫码登录", 400)

    probe_video = None
    if body.get("video"):
        candidate = resolve_video_path(body["video"])
        if not Path(candidate).is_file():
            return _err(f"测试视频不存在: {candidate}", 400)
        probe_video = candidate
    try:
        with browser_session(af):
            report = _run_async(
                lambda: xiaohongshu_selftest(
                    str(af),
                    headless=bool(body.get("headless", True)),
                    pages=tuple(body.get("pages") or ("login", "publish")),
                    probe_video=probe_video,
                ),
                timeout=600,
            )
        return _ok(report.to_dict())
    except Exception as exc:
        return _err(f"自检失败: {exc}")


@xhs_bp.post("/collections")
def xhs_collections_api():
    """读取账号可选合集。

    ⚠️ 关键约束：创作平台**没有独立的合集管理页**（已实测 /collection、/note-manager 等
    多个入口都不存在），唯一入口是发布页的「选择合集」弹层，而它**必须先上传视频并等转码
    完成**才会渲染。所以真实读取的代价是分钟级的。

    因此策略是：
    - 传 `video` → 真实读取（上传测试视频，**不会发布**），成功后写入缓存；
    - 不传 `video` → 直接返回缓存，秒回。
    """
    body = request.get_json(silent=True) or {}
    account = body.get("account")
    if not account:
        return _err("缺少 account", 400)
    af = account_file_for(account)
    if not af.exists():
        return _err("该账号还没有 cookie，请先扫码登录", 400)

    # 不传视频 → 走缓存
    if not body.get("video"):
        cached = _load_collections_cache(account)
        if cached:
            cols = cached.get("collections") or []
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(cached.get("cached_at", 0)))
            return _ok(
                {
                    "step": "list_collections",
                    "status": "ok",
                    "message": f"来自缓存：{len(cols)} 个合集（{when} 读取）",
                    "detail": {"collections": cols, "trigger": "cache"},
                    "cached": True,
                    "cached_at": cached.get("cached_at"),
                }
            )
        return _err(
            "还没有合集缓存。创作平台没有独立的合集管理页，读合集需要在发布页上传一个视频"
            "（不会发布），请选一个测试视频后点「刷新合集」。",
            400,
        )

    candidate = resolve_video_path(body["video"])
    if not Path(candidate).is_file():
        return _err(f"测试视频不存在: {candidate}", 400)
    try:
        with browser_session(af):
            result = _run_async(
                lambda: xiaohongshu_list_collections(
                    str(af),
                    headless=bool(body.get("headless", True)),
                    probe_video=candidate,
                ),
                timeout=900,
            )
        if result.ok:
            _save_collections_cache(account, (result.detail or {}).get("collections") or [])
        return _ok(result.to_dict())
    except Exception as exc:
        return _err(f"获取合集失败: {exc}")


# --------------------------------------------------------------------------- #
# 发布
# --------------------------------------------------------------------------- #


@xhs_bp.post("/publish")
def xhs_publish_api():
    """阶段一：提交任务（**不直接发布**）。

    两阶段流程：
      1. 提交 → 入队 → 若开启 AI 则生成文案 → 状态变 `pending_confirm`（待确认）；
      2. 人工在任务列表里核对/修改文案 → 点「确认发布」→ 才真正驱动浏览器发布。

    这样"点提交"和"真发出去"之间有一道人工闸门：AI 写得不对也不会直接发出去。
    """
    body = request.get_json(silent=True) or {}
    account = body.get("account")
    video = body.get("video")
    title = (body.get("title") or "").strip()
    ai_generate = bool(body.get("ai_generate", True))

    if not account:
        return _err("缺少 account", 400)
    if not video:
        return _err("缺少 video（素材文件名或绝对路径）", 400)
    # 交给 AI 生成时，标题可以先留空，等生成完再确认
    if not title and not ai_generate:
        return _err("未开启 AI 生成时标题必填（不超过 20 字）", 400)

    af = account_file_for(account)
    if not af.exists():
        return _err("该账号还没有 cookie，请先扫码登录", 400)

    video_path = resolve_video_path(video)
    if not Path(video_path).is_file():
        return _err(f"视频文件不存在: {video_path}", 400)

    schedule_raw = (body.get("schedule") or "").strip()
    if schedule_raw:
        try:
            datetime.strptime(schedule_raw, "%Y-%m-%d %H:%M")
        except ValueError:
            return _err("定时时间格式应为 YYYY-MM-DD HH:MM", 400)

    task_id = uuid.uuid4().hex
    with _tasks_lock:
        _tasks[task_id] = {
            "id": task_id,
            "state": "analyzing" if ai_generate else "pending_confirm",
            "stage": "generate" if ai_generate else "confirm",
            "message": "AI 正在生成文案…" if ai_generate else "等待人工确认后发布",
            "account": account,
            "account_file": str(af),
            "video": video_path,
            "video_name": Path(video_path).name,
            "title": title,
            "desc": (body.get("desc") or "").strip(),
            "tags": [str(t).strip().lstrip("#") for t in (body.get("tags") or []) if str(t).strip()][:10],
            "hints": (body.get("hints") or "").strip(),
            "ai_generate": ai_generate,
            "collection": (body.get("collection") or "").strip() or None,
            "visibility": (body.get("visibility") or "").strip() or None,
            "original": bool(body.get("original")),
            "schedule": schedule_raw or None,
            "thumbnail": body.get("thumbnail") or None,
            "dry_run": bool(body.get("dry_run")),
            "scheduled": bool(schedule_raw),
            "debug": bool(body.get("debug", False)),
            # 提交阶段就把模式钉死成有头：发布不允许无头（见确认发布处的说明）。
            # 这里也忽略前端传进来的值，避免"提交时无头、确认时被绕过"。
            "headless": False,
            "created_at": time.time(),
            "updated_at": time.time(),
            "steps": [],
        }

    threading.Thread(target=_generate_worker, args=(task_id,), daemon=True).start()
    return _ok({"task_id": task_id, "state": _tasks[task_id]["state"]})


# --------------------------------------------------------------------------- #
# 素材文案（AI 生成的标题/正文/话题，跟着素材走）
# --------------------------------------------------------------------------- #
#
# 单独开一张表而不是往上游的 file_records 上加列：那是上游的表，加列会让后续跟进上游变麻烦。
# 主键用磁盘文件名，素材重命名时同步改 key。

def _material_db():
    return sqlite3.connect(Path(BASE_DIR) / "db" / "database.db")


def _ensure_copy_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS material_copy (
            file_path  TEXT PRIMARY KEY,
            title      TEXT,
            desc       TEXT,
            tags       TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def save_material_copy(disk_name: str, title: str, desc: str, tags: list[str]) -> None:
    if not disk_name:
        return
    try:
        with _material_db() as conn:
            _ensure_copy_table(conn)
            conn.execute(
                """
                INSERT INTO material_copy (file_path, title, desc, tags, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(file_path) DO UPDATE SET
                    title = excluded.title,
                    desc  = excluded.desc,
                    tags  = excluded.tags,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (disk_name, title or "", desc or "", ",".join(tags or [])),
            )
            conn.commit()
    except Exception as exc:
        print(f"⚠️ 保存素材文案失败（不影响发布）: {exc}")


def rename_material_copy(old_name: str, new_name: str) -> None:
    if not old_name or not new_name or old_name == new_name:
        return
    try:
        with _material_db() as conn:
            _ensure_copy_table(conn)
            # 先把新名字上的残留行删掉，再改过去。
            # 素材删除时不会自动清理文案表（见 delete_material_copy 的说明），
            # 直接 UPDATE 撞上主键会抛异常，结果文案留在旧名字下——
            # 表现就是"素材明明生成了文案，列表里却显示无"。
            conn.execute("DELETE FROM material_copy WHERE file_path = ?", (new_name,))
            conn.execute("UPDATE material_copy SET file_path = ? WHERE file_path = ?", (new_name, old_name))
            conn.commit()
    except Exception as exc:
        print(f"⚠️ 同步素材文案路径失败: {exc}")


def delete_material_copy(disk_name: str) -> None:
    """删素材时一起删掉它的 AI 文案。

    现在文件名就是标题，重名概率比 uuid 时代高得多：残留的文案行会在
    下一个同名素材上"复活"，让新素材平白显示成"已有文案"。
    """
    if not disk_name:
        return
    try:
        with _material_db() as conn:
            _ensure_copy_table(conn)
            conn.execute("DELETE FROM material_copy WHERE file_path = ?", (disk_name,))
            conn.commit()
    except Exception as exc:
        print(f"⚠️ 删除素材文案失败（不影响素材删除）: {exc}")


def load_material_copy(disk_name: str) -> dict | None:
    try:
        with _material_db() as conn:
            conn.row_factory = sqlite3.Row
            _ensure_copy_table(conn)
            row = conn.execute("SELECT * FROM material_copy WHERE file_path = ?", (disk_name,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["tags"] = [x for x in (d.get("tags") or "").split(",") if x]
            return d
    except Exception:
        return None


def all_material_copies() -> dict:
    out = {}
    try:
        with _material_db() as conn:
            conn.row_factory = sqlite3.Row
            _ensure_copy_table(conn)
            for row in conn.execute("SELECT * FROM material_copy"):
                d = dict(row)
                d["tags"] = [x for x in (d.get("tags") or "").split(",") if x]
                out[d["file_path"]] = d
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- #
# 素材重命名（AI 出标题后，把素材文件改成标题名）
# --------------------------------------------------------------------------- #

# 文件名白名单：**只保留中文和数字**。
# 模型给的标题里常带逗号、感叹号、emoji、空格甚至英文词，这些字符在跨平台拷贝、
# 平台上传、命令行/SQL 里都容易出问题，索性按白名单过滤，其余一律丢掉。
_FILENAME_KEEP_RE = re.compile(r"[^\u4e00-\u9fff0-9]")


def _safe_title_for_filename(title: str, max_len: int = 50) -> str:
    """把标题变成文件名片段：**只允许中文和数字**。

    空格、标点、emoji、英文全部去掉（路径分隔符自然也一起去掉了，不会再跑到别的目录去）。
    扩展名不在这里处理，由调用方拼上。
    """
    return _FILENAME_KEEP_RE.sub("", title or "")[:max_len]


def sync_file_record(old_disk_name: str, new_disk_name: str, display_name: str) -> None:
    """同步 file_records，让「素材管理」里显示标题名。

    没有记录就补一条——否则重命名后素材管理里依然看不到这个文件。

    公开命名（不带下划线）是因为**背景音乐合成**（`sau_backend_music.py`）改名后
    也要走这一步：两边口径必须一致（display_name/file_path/filesize 一起改），
    各写一份迟早会漏字段。
    """
    disk = Path(BASE_DIR) / "videoFile" / new_disk_name
    size_mb = round(disk.stat().st_size / (1024 * 1024), 2)
    try:
        with sqlite3.connect(Path(BASE_DIR) / "db" / "database.db") as conn:
            cur = conn.execute(
                "UPDATE file_records SET filename = ?, file_path = ?, filesize = ? WHERE file_path = ?",
                (display_name, new_disk_name, size_mb, old_disk_name),
            )
            if cur.rowcount == 0:
                conn.execute(
                    "INSERT INTO file_records (filename, filesize, file_path) VALUES (?, ?, ?)",
                    (display_name, size_mb, new_disk_name),
                )
            conn.commit()
    except Exception as exc:
        print(f"⚠️ 重命名成功但同步 file_records 失败（素材管理可能不更新）: {exc}")


def _rename_material_to_title(video_path: str, title: str) -> tuple[str | None, str]:
    """把素材文件重命名成标题名，返回 (新路径 or None, 说明)。

    命名规则：**文件名就是标题**，即 `<标题>.mp4`，只保留中文和数字（见
    `_safe_title_for_filename`），不带 uuid 前缀、不带任何符号。
    重名时在标题后面补数字：`标题2.mp4`、`标题3.mp4`（仍然是纯中文+数字）。

    例外：**已经换过背景音乐**的素材会保留 `-音乐名` 后缀（`标题-音乐名.mp4`）。
    不带的话，一次"生成文案"会同时抹掉两样东西：用户再也看不出用的哪首音乐，
    而 `material_bgm` 那行还挂在旧名字上（素材管理里退回「待合成」，但音频其实已经是那首）。
    """
    src = Path(video_path)
    if not src.is_file():
        return None, "原文件不存在"

    safe = _safe_title_for_filename(title)
    if not safe:
        return None, "标题里没有中文或数字，文件名未改动"

    base, music_name = _title_with_music_suffix(safe, src.name)

    ext = src.suffix or ".mp4"
    target = src.with_name(f"{base}{ext}")
    if target == src:
        return str(src), "文件名已是标题，未改动"

    n = 2
    while target.exists():
        target = src.with_name(f"{base}{n}{ext}")
        n += 1
        if n > 99:
            return None, "同名文件过多，放弃重命名"

    src.rename(target)
    rename_material_copy(src.name, target.name)
    # 展示名同步成新文件名（**带扩展名**：素材管理靠后缀判断是视频还是图片，
    # 不带后缀会被判成"未知类型"，预览就只剩一行文件信息、没有播放器）
    sync_file_record(src.name, target.name, target.name)
    # 背景音乐状态跟着搬（没有音乐后缀说明没合成过/合成失败，把旧名字上的残留清掉）
    if music_name:
        _rename_bgm_status(src.name, target.name)
    else:
        _delete_bgm_status(src.name)
    return str(target), f"素材已重命名为「{target.name}」"


def _title_with_music_suffix(safe_title: str, disk_name: str) -> tuple[str, str]:
    """给"标题"补上已有的 `-音乐名` 后缀，返回 `(文件名主名, 音乐名)`。

    只认**已合成**（`done`）的状态：待合成/失败的素材没有成品音轨，加后缀只会误导。
    延迟 import：这个模块被 CLI 等入口导入时不一定要拉起背景音乐那套东西。
    """
    try:
        from myUtils.background_music import STATUS_DONE, get_status, safe_music_segment

        status = get_status(disk_name)
        if status.get("status") == STATUS_DONE and status.get("music_name"):
            segment = safe_music_segment(status["music_name"])
            if segment:
                return f"{safe_title}-{segment}", segment
    except Exception as exc:
        print(f"⚠️ 读取背景音乐状态失败（改名时按未合成处理）: {exc}")
    return safe_title, ""


def _rename_bgm_status(old_name: str, new_name: str) -> None:
    try:
        from myUtils.background_music import rename_status

        rename_status(old_name, new_name)
    except Exception as exc:
        print(f"⚠️ 同步背景音乐状态失败（不影响改名）: {exc}")


def _delete_bgm_status(disk_name: str) -> None:
    try:
        from myUtils.background_music import delete_status

        delete_status(disk_name)
    except Exception as exc:
        print(f"⚠️ 清理背景音乐状态失败（不影响改名）: {exc}")


def _generate_worker(task_id: str) -> None:
    """阶段一：AI 生成文案。完成后进入 `pending_confirm`，等人确认。"""
    snapshot = _task_snapshot(task_id)
    if snapshot is None:
        return

    if not snapshot.get("ai_generate"):
        _task_update(task_id, state="pending_confirm", message="等待人工确认后发布")
        return

    try:
        res = generate_note_copy(snapshot["video"], snapshot.get("hints") or "")
    except Exception as exc:
        _task_update(
            task_id,
            state="pending_confirm",
            message=f"AI 生成异常：{exc} — 请手动填写后确认发布",
            ai_error=str(exc),
        )
        return

    if not res.get("ok"):
        _task_update(
            task_id,
            state="pending_confirm",
            message=f"AI 生成失败：{res.get('error')} — 请手动填写后确认发布",
            ai_error=res.get("error"),
        )
        return

    patch = {
        "state": "pending_confirm",
        "message": "AI 已生成文案，请核对后点「确认发布」",
        "generated_at": time.time(),
        "ai_model": res.get("model"),
        "frames": res.get("frames"),
        "ai_error": None,
    }
    # 用户手填过的内容优先，AI 只补空缺
    if not (snapshot.get("title") or "").strip():
        patch["title"] = res["title"]
    if not (snapshot.get("desc") or "").strip():
        patch["desc"] = res["desc"]
    if not (snapshot.get("tags") or []):
        patch["tags"] = res["tags"]

    # 有了标题就把素材文件也改成标题名：
    # 素材管理里一眼能认出是哪个视频，后面发布用的也是这个新路径。
    final_title = (patch.get("title") or snapshot.get("title") or "").strip()
    if final_title:
        new_path, note = _rename_material_to_title(snapshot["video"], final_title)
        patch["rename_note"] = note
        if new_path:
            patch["video"] = new_path
            patch["video_name"] = Path(new_path).name
        if note.startswith("素材已重命名"):
            patch["message"] = f"AI 已生成文案，{note}；请核对后点「确认发布」"

    # 把文案挂到素材上：发布中心选中素材时就能自动带出标题和正文
    save_material_copy(
        Path(patch.get("video") or snapshot["video"]).name,
        patch.get("title") or snapshot.get("title") or "",
        patch.get("desc") or snapshot.get("desc") or "",
        patch.get("tags") or snapshot.get("tags") or [],
    )

    _task_update(task_id, **patch)


@xhs_bp.post("/task/<task_id>/regenerate")
def xhs_task_regenerate(task_id: str):
    """重新生成文案（仅「待确认」状态可用）。"""
    snapshot = _task_snapshot(task_id)
    if snapshot is None:
        return _err("任务不存在（后端可能已重启）", 404)
    if snapshot.get("state") != "pending_confirm":
        return _err(f"当前状态为 {snapshot.get('state')}，只有「待确认」的任务能重新生成", 400)

    body = request.get_json(silent=True) or {}
    patch: dict = {"state": "analyzing", "message": "AI 正在重新生成文案…"}
    if "hints" in body:
        patch["hints"] = (body.get("hints") or "").strip()
    # 清掉旧文案，否则"AI 只补空缺"的逻辑会把上一版留下来
    patch.update({"title": "", "desc": "", "tags": []})
    _task_update(task_id, **patch)
    threading.Thread(target=_generate_worker, args=(task_id,), daemon=True).start()
    return _ok({"task_id": task_id})


@xhs_bp.post("/task/<task_id>/confirm")
def xhs_task_confirm(task_id: str):
    """阶段二：人工确认后**才真正发布**。请求体里可以带上改好的文案。"""
    snapshot = _task_snapshot(task_id)
    if snapshot is None:
        return _err("任务不存在（后端可能已重启）", 404)
    if snapshot.get("state") != "pending_confirm":
        return _err(f"当前状态为 {snapshot.get('state')}，只有「待确认」的任务能确认发布", 400)

    body = request.get_json(silent=True) or {}
    patch: dict = {}
    if "title" in body:
        patch["title"] = (body.get("title") or "").strip()
    if "desc" in body:
        patch["desc"] = (body.get("desc") or "").strip()
    if "tags" in body:
        patch["tags"] = [str(t).strip().lstrip("#") for t in (body.get("tags") or []) if str(t).strip()][:10]
    for key in ("collection", "visibility", "schedule"):
        if key in body:
            patch[key] = (body.get(key) or "").strip() or None
    if "original" in body:
        patch["original"] = bool(body.get("original"))
    if "dry_run" in body:
        patch["dry_run"] = bool(body.get("dry_run"))

    # 用确认后的最终值做校验（而不是提交时的旧值）
    final_title = (patch.get("title", snapshot.get("title") or "") or "").strip()
    if not final_title:
        return _err("标题不能为空，请填写后再确认发布", 400)
    if len(final_title) > 20:
        return _err(f"标题超过 20 字（当前 {len(final_title)} 字）", 400)
    schedule_raw = (patch.get("schedule", snapshot.get("schedule")) or "").strip()
    if schedule_raw:
        try:
            datetime.strptime(schedule_raw, "%Y-%m-%d %H:%M")
        except ValueError:
            return _err("定时时间格式应为 YYYY-MM-DD HH:MM", 400)

    # 浏览器模式是**固定**的：小红书发布一律走有头（本机弹窗），不再是可选档。
    # 原因：2026-03-10 小红书《关于打击AI托管运营账号的治理公告》明确禁止"利用技术
    # 手段模拟真人"，并对"与真人操作习惯不符"的行为特征分档处罚。无头发布是这条链路
    # 上最扎眼的特征，所以直接砍掉这个选项——就连前端传了 headless=true 也一律覆盖，
    # 避免绕过。只读操作（扫码登录 / 自检 / 列合集）不受影响。
    headless = False
    mode_text = "有头浏览器（本机弹窗）"
    patch.update(
        {
            "state": "queued",
            "stage": "publish",
            # 把有头写回任务快照：worker/发布记录/前端都读这个字段，
            # 不能只改局部变量，否则旧值（可能是在无头档下建的）会留在快照里。
            "headless": headless,
            "message": f"已确认（{mode_text}发布），排队等待浏览器",
            "scheduled": bool(schedule_raw),
            "confirmed_at": time.time(),
        }
    )
    # 到这一步才真的会发出去，所以现在才开记录：
    # 「待确认」和「已放弃」的任务不算发布过，不该在「发布记录」里占位置。
    patch["record_id"] = _open_publish_record(snapshot, patch, mode_text=mode_text)
    _task_update(task_id, **patch)
    threading.Thread(target=_publish_worker, args=(task_id,), daemon=True).start()
    return _ok({"task_id": task_id, "record_id": patch["record_id"]})


def _open_publish_record(snapshot: dict, patch: dict, mode_text: str = "无头浏览器") -> int:
    """为工作台确认发布的这条笔记开一条发布记录，返回 record_id。"""
    video_path = Path(patch.get("video") or snapshot.get("video") or "")
    account_file = Path(snapshot.get("account_file") or "")
    try:
        return records.insert_record(
            records.new_record(
                task_id=snapshot.get("id") or "",
                source=records.SOURCE_XHS_WORKBENCH,
                platform=1,
                platform_name="小红书",
                # 工作台的账号是 cookies/ 下的 cookie 名（自己扫码登录出来的），
                # 和发布中心 user_info 里的账号不是一套，所以 account_id 留空。
                account_name=snapshot.get("account") or "",
                account_file=account_file.name,
                material=video_path.name,
                material_title=video_path.name,
                title=(patch.get("title") or snapshot.get("title") or "").strip(),
                desc=(patch.get("desc") or snapshot.get("desc") or "").strip(),
                tags=patch.get("tags") or snapshot.get("tags") or [],
                scheduled=bool((patch.get("schedule") or snapshot.get("schedule") or "").strip()),
                schedule_at=(patch.get("schedule") or snapshot.get("schedule") or "").strip(),
                is_original=bool(patch.get("original", snapshot.get("original"))),
                # 记录里带上浏览器模式：出了风控提示时能回头看出这条是哪种模式跑的
                action=f"已确认（{mode_text}发布），排队等待浏览器",
                message=f"浏览器模式：{mode_text}",
            )
        )
    except Exception as exc:
        # 记录写不进去不该挡住发布；但要说清楚，免得出现"发了却没记录"
        print(f"⚠️ 创建发布记录失败（发布照常进行）: {exc}")
        return 0


@xhs_bp.post("/task/<task_id>/cancel")
def xhs_task_cancel(task_id: str):
    """放弃一个还没真正发布的任务。"""
    snapshot = _task_snapshot(task_id)
    if snapshot is None:
        return _err("任务不存在（后端可能已重启）", 404)
    if snapshot.get("state") not in ("analyzing", "pending_confirm"):
        return _err(f"当前状态为 {snapshot.get('state')}，无法放弃", 400)
    _task_update(task_id, state="cancelled", message="已放弃", finished_at=time.time())
    return _ok({"task_id": task_id})


def _xhs_timeout_message() -> str:
    minutes = XHS_TASK_TIMEOUT / 60
    shown = f"{minutes:.0f} 分钟" if minutes >= 1 else f"{XHS_TASK_TIMEOUT:.0f} 秒"
    return (
        f"执行超时：超过 {shown} 没有结束，已判定卡住。"
        f"这类卡住多半是在等一个不再响应的浏览器，线程无法被安全中断，"
        f"它可能还占着这个账号——建议重启后端回收资源后重试。"
    )


def _arm_xhs_watchdog(
    task_id: str, record_id: int, snapshot: dict, log_sink: list[dict], started_at: float
) -> threading.Timer:
    """给工作台的发布任务装超时看门狗；正常结束必须用 `_disarm_xhs_watchdog` 取消。

    和发布中心（`sau_backend_publish._arm_watchdog`）同样的道理：卡住的线程收不回来，
    但**结论必须改对**，否则界面上永远是「发布中」，用户既不知道发生了什么、
    也没法判断能不能重试。

    工作台这边没有发布中心那种"通道队列"，每个任务自己一个线程，所以看门狗不需要
    再通知谁往下走，只要把任务和发布记录收尾即可。
    """

    def _on_timeout() -> None:
        with _tasks_lock:
            task = _tasks.get(task_id)
            if task is None or task.get("state") not in XHS_ACTIVE_STATES:
                return  # 已经正常收尾，或压根还没开始跑
            app = task.get("_app")
        steps = [r.to_dict() for r in getattr(app, "step_results", [])] if app else []
        message = _xhs_timeout_message()
        if not _task_finish(
            task_id, state="failed", message=message, steps=steps, _from_watchdog=True
        ):
            return
        print(f"⏱️ 小红书工作台任务执行超时，判定卡住: {task_id}")
        try:
            _finish_record(record_id, snapshot, steps, log_sink, started_at, steps, message)
        except Exception as exc:  # 写记录失败不能反过来影响任务结论
            print(f"⚠️ 超时任务写发布记录失败（不影响任务判定）: {exc}")

    timer = threading.Timer(XHS_TASK_TIMEOUT, _on_timeout)
    timer.daemon = True
    timer.name = f"xhs-watchdog-{task_id[:8]}"
    timer.start()
    return timer


def _disarm_xhs_watchdog(timer: threading.Timer | None) -> None:
    if timer is not None:
        timer.cancel()


def _publish_worker(task_id: str) -> None:
    """阶段二：真正驱动浏览器发布。"""
    snapshot = _task_snapshot(task_id)
    if snapshot is None:
        return

    # 浏览器操作串行（同账号多会话会互相踢下线）：先标排队，拿到锁再转 running
    # 模式固定有头：发布不允许无头，这里直接钉死，不读 snapshot 里的旧值
    headless = False
    mode_text = "有头浏览器（本机弹窗）"
    _task_update(task_id, state="queued", message=f"排队中（浏览器操作串行执行，{mode_text}）")
    record_id = int(snapshot.get("record_id") or 0)
    log_sink: list[dict] = []
    started_at = time.time()
    if record_id:
        records.update_record(
            record_id,
            status=records.STATUS_RUNNING,
            stage=progress.STAGE_LAUNCHING,
            stage_text=progress.STAGE_TEXT[progress.STAGE_LAUNCHING],
            action=f"排队中（浏览器操作串行执行，{mode_text}）",
            started_at=records.now_text(started_at),
        )
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = None
    watchdog: threading.Timer | None = None
    try:
        # 看门狗从这里起算（任务真正开始执行）；正常收尾时会把它取消。
        # 装在 try 里面而不是外面：万一装表本身就失败，异常会被下面的 except 接住、
        # 把任务判失败；装在外面的话线程会直接死掉，任务永远停在「queued」——那正是
        # 这次要消灭的"静默卡住"。
        watchdog = _arm_xhs_watchdog(task_id, record_id, snapshot, log_sink, started_at)
        schedule_raw = (snapshot.get("schedule") or "").strip()
        schedule_at: datetime | int = 0
        if schedule_raw:
            schedule_at = datetime.strptime(schedule_raw, "%Y-%m-%d %H:%M")

        app = XiaoHongShuVideo(
            title=snapshot.get("title") or "",
            file_path=snapshot["video"],
            desc=snapshot.get("desc") or "",
            tags=snapshot.get("tags") or [],
            publish_date=schedule_at,
            account_file=snapshot.get("account_file") or str(account_file_for(snapshot["account"])),
            thumbnail_path=snapshot.get("thumbnail") or None,
            publish_strategy=(
                XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED
                if schedule_raw
                else XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE
            ),
            debug=bool(snapshot.get("debug")),
            headless=headless,
            collection_name=snapshot.get("collection") or None,
            visibility=snapshot.get("visibility") or None,
            is_original=bool(snapshot.get("original")),
            dry_run=bool(snapshot.get("dry_run")),
        )
        with _tasks_lock:
            _tasks[task_id]["_app"] = app
        account_path = snapshot.get("account_file") or str(account_file_for(snapshot["account"]))
        with browser_session(account_path, on_wait=lambda text: _task_update(task_id, message=text)):
            running_msg = f"{mode_text}已启动，正在执行页面操作"
            _task_update(task_id, state="running", message=running_msg)
            if record_id:
                records.update_record(record_id, action=running_msg)
            # 发布记录也要能看到"此刻在干什么"：和发布中心用同一套进度桥，
            # 显式阶段 + 上传器日志旁听（这里跑在 worker 自己的线程里，日志不会串台）
            progress.bind(_record_progress(record_id, log_sink), "xiaohongshu")
            try:
                loop.run_until_complete(app.main())
            finally:
                progress.unbind()
        steps = [r.to_dict() for r in getattr(app, "step_results", [])]
        failed = [s for s in steps if s["status"] == "failed"]
        message = "；".join(s["message"] for s in failed) if failed else "完成"
        # 看门狗已经判过卡死的话这里会返回 False，那就连发布记录也别再写——
        # 否则任务和记录会各说各话
        if _task_finish(task_id, state="failed" if failed else "success", message=message, steps=steps):
            _finish_record(record_id, snapshot, steps, log_sink, started_at, failed, message)
    except Exception as exc:
        steps = [r.to_dict() for r in getattr(app, "step_results", [])] if app else []
        if _task_finish(task_id, state="failed", message=str(exc), steps=steps):
            _finish_record(record_id, snapshot, steps, log_sink, started_at, steps, str(exc))
    finally:
        _disarm_xhs_watchdog(watchdog)
        progress.unbind()
        loop.close()


# --------------------------------------------------------------------------- #
# 发布记录（工作台这一侧的写入；表和状态词都在 myUtils/publish_records.py）
# --------------------------------------------------------------------------- #


def _record_progress(record_id: int, log_sink: list[dict]):
    """把进度事件写进发布记录。连续重复的日志行丢掉，别把日志尾巴刷满。"""

    def handler(event: dict) -> None:
        if not record_id:
            return
        text = (event.get("text") or "").strip()
        fields: dict = {}
        if event.get("type") == "log":
            if not text:
                return
            if log_sink and log_sink[-1]["text"] == text:
                return
            log_sink.append(
                {"t": records.now_text(), "text": text[:records.LOG_LINE_MAX], "level": event.get("level", "INFO")}
            )
            del log_sink[:-records.LOG_TAIL_LINES]
            fields["action"] = text[:records.LOG_LINE_MAX]
        stage = event.get("stage")
        if stage:
            fields["stage"] = stage
            fields["stage_text"] = progress.STAGE_TEXT.get(stage, "")
        if fields:
            try:
                records.update_record(record_id, **fields)
            except Exception as exc:  # 记进度失败绝不能影响发布本身
                print(f"⚠️ 写发布记录进度失败（不影响发布）: {exc}")

    return handler


def _finish_record(
    record_id: int,
    snapshot: dict,
    steps: list,
    log_sink: list[dict],
    started_at: float,
    failed: list,
    message: str,
) -> None:
    """发布结束：把最终状态、逐步结果和日志尾巴落进记录。"""
    if not record_id:
        return
    try:
        verified = any(s.get("step") == "verify_published" and s.get("status") == "ok" for s in steps)
        if failed:
            status = records.STATUS_FAILED
        elif snapshot.get("dry_run"):
            status = records.STATUS_SUCCESS
            message = "演练完成（dry-run，未真正发布）"
        elif snapshot.get("scheduled"):
            status = records.STATUS_SUCCESS
            message = f"已定时提交（{snapshot.get('schedule')}）"
        elif verified:
            status = records.STATUS_SUCCESS
            message = "已发布成功"
        else:
            status = records.STATUS_SUCCESS
            message = "已提交，建议到平台确认发布结果"
        # 模式固定有头（发布不再提供无头档），这里如实记录，便于回溯
        mode_text = "有头浏览器"
        mode_note = f"（{mode_text}）"
        records.update_record(
            record_id,
            status=status,
            stage=status,
            stage_text=records.STATUS_TEXT.get(status, status),
            action=f"{message}{mode_note}",
            message=f"{message}{mode_note}",
            finished_at=records.now_text(),
            duration=round(time.time() - started_at, 1),
            steps=json.dumps(records.json_safe(steps), ensure_ascii=False),
            log=json.dumps(records.json_safe(log_sink[-records.LOG_TAIL_LINES:]), ensure_ascii=False),
        )
    except Exception as exc:
        print(f"⚠️ 写发布记录结果失败（发布本身已完成）: {exc}")


@xhs_bp.get("/task/<task_id>")
def xhs_task(task_id: str):
    snapshot = _task_snapshot(task_id)
    if snapshot is None:
        return _err("任务不存在（后端可能已重启）", 404)
    return _ok(snapshot)


@xhs_bp.get("/materials")
def xhs_materials():
    """列出素材库里的视频文件。

    直接读 `videoFile/` 目录，而不是查上游的 `file_records` 表——
    上游 `/upload` 存了文件却不写库，`/getFiles` 因此看不到新上传的文件。
    """
    video_dir = Path(BASE_DIR) / "videoFile"
    copies = all_material_copies()
    items = []
    if video_dir.is_dir():
        for f in sorted(video_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.is_file() and not f.name.startswith("."):
                stat = f.stat()
                copy = copies.get(f.name) or {}
                items.append(
                    {
                        "name": f.name,
                        "size": stat.st_size,
                        "size_mb": round(stat.st_size / 1048576, 1),
                        "mtime": stat.st_mtime,
                        "mtime_text": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
                        "abs_path": str(f),
                        # AI 文案（没有则空，发布中心会提示先生成）
                        "title": copy.get("title") or "",
                        "desc": copy.get("desc") or "",
                        "tags": copy.get("tags") or [],
                        "has_copy": bool(copy.get("desc")),
                    }
                )
    return _ok(items)


@xhs_bp.get("/tasks")
def xhs_tasks():
    with _tasks_lock:
        ids = list(_tasks.keys())
    items = [s for s in (_task_snapshot(i) for i in ids) if s]
    items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return _ok(items[:50])


# --------------------------------------------------------------------------- #
# 模型设置（OpenAI 兼容格式：url / key / model）
# --------------------------------------------------------------------------- #


@xhs_bp.get("/settings")
def xhs_get_settings():
    """读取设置。API Key 只返回掩码，不回显明文。"""
    return _ok(public_settings())


@xhs_bp.post("/settings")
def xhs_save_settings():
    """保存设置。

    约定：`vision.api_key` 传空字符串 = 不修改（前端不回显明文，所以不能拿空值覆盖掉）。
    要清空 key 请显式传 `"vision": {"api_key": null}`。
    """
    body = request.get_json(silent=True) or {}
    try:
        save_settings(body)
    except Exception as exc:
        return _err(f"保存设置失败: {exc}")
    return _ok(public_settings(), "已保存")


@xhs_bp.post("/settings/test")
def xhs_test_settings():
    """连通性测试：发一个最小的文本请求，验证 url / key / model 三个要素是否可用。"""
    body = request.get_json(silent=True) or {}
    if body.get("vision"):
        # 支持"先测试再保存"：把临时配置合并进去
        save_settings(body)
    cfg = load_settings().get("vision") or {}
    if not (cfg.get("base_url") or "").strip():
        return _err("请先填写服务地址（base_url）", 400)
    if not (cfg.get("model") or "").strip():
        return _err("请先填写模型名（model）", 400)
    try:
        text = chat_completion(
            [
                {"role": "system", "content": "你是连通性测试助手，只用一句话回答。"},
                {"role": "user", "content": "回复：连接成功"},
            ],
            max_tokens=32,
        )
        return _ok(
            {
                "ok": True,
                "base_url": cfg.get("base_url"),
                "model": cfg.get("model"),
                "reply": (text or "").strip()[:200],
            },
            "连接成功",
        )
    except Exception as exc:
        return _err(f"连接失败: {exc}")


# --------------------------------------------------------------------------- #
# AI 生成文案（首帧 + 尾帧 → 视觉模型）
# --------------------------------------------------------------------------- #


@xhs_bp.post("/analyze")
def xhs_analyze():
    """抽取视频首帧/尾帧，交给多模态模型生成标题、正文、话题。

    只读视频文件，不碰小红书账号，所以不需要 cookie、也不占用浏览器锁。

    `save=true` 时还会把结果**落到素材上**：写 material_copy，并把素材文件改名成标题
    （文件名只保留中文和数字，见 `_safe_title_for_filename`）——
    和发布流程里 AI 生成文案后的行为保持一致。改完的新文件名会随响应返回，
    调用方必须用 `video_name` 覆盖自己手里的旧名字，否则后续会指向不存在的文件。
    """
    body = request.get_json(silent=True) or {}
    video = body.get("video")
    if not video:
        return _err("缺少 video（素材文件名或绝对路径）", 400)

    video_path = resolve_video_path(video)
    if not Path(video_path).is_file():
        return _err(f"视频文件不存在: {video_path}", 400)

    try:
        result = generate_note_copy(video_path, hints=(body.get("hints") or "").strip())
    except Exception as exc:
        return _err(f"生成失败: {exc}")

    if not result.get("ok"):
        return _err(result.get("error") or "生成失败", 500, {"frames": result.get("frames")})

    video_name = Path(video_path).name
    rename_note = ""
    # 发布中心/素材管理可以对素材直接生成并留存文案，之后选中素材就能自动带出
    if body.get("save"):
        # 先存文案（此刻还挂在旧文件名下）再改名：改名会把文案行一起迁到新名字，
        # 万一改名失败，文案已经存下来了，不会两边都丢。
        save_material_copy(video_name, result["title"], result["desc"], result["tags"])
        new_path, rename_note = _rename_material_to_title(video_path, result["title"])
        if new_path:
            video_name = Path(new_path).name

    return _ok(
        {
            "title": result["title"],
            "desc": result["desc"],
            "tags": result["tags"],
            "model": result.get("model"),
            "frames": result.get("frames"),
            "raw": (result.get("raw") or "")[:2000],
            # 素材在磁盘上现在的文件名（改过名就是改名后的），调用方拿它更新自己的引用
            "video_name": video_name,
            "rename_note": rename_note,
        },
        "生成成功",
    )


@xhs_bp.get("/defaults")
def xhs_defaults():
    """发布页默认值（例如默认合集）。"""
    return _ok({"default_collection": get_default_collection()})
