# -*- coding: utf-8 -*-
"""账号会话定时续期（at-ai-xhs 改造新增）。

为什么需要它
------------
各平台的登录态都是**服务端会话**，本地 cookie 文件上那个 `expires`（常常写到一年后）
跟它什么时候被作废毫无关系。视频号那边已经实测过：晚上扫码、当晚校验还有效，
第二天早上服务端会话就没了，而文件里还写着 2027 年（见 docs/视频号会话保活.md）。

拖住它的办法只有一个：**定期真的去用一次**——带着 cookie 打开一次创作者后台，
服务端就会把票续上。这就是这里的「续期」，做的动作和「账号管理」页点刷新时逐个账号
校验是同一件事（`check_cookie`），区别只在于由后端**按配置的时间点自动做**，
而且会把服务端轮换下来的新票据写回 cookie 文件（`check_cookie(..., save_state=True)`）。

时间点
------
默认 **00:00 / 07:00 / 14:00 / 20:00**，默认开启；在「设置 → 账号会话续期」里可以改，
也可以在「账号管理」页那个「定时续期」按钮上直接开关。配置存 settings.json 的
`session_renew` 段（见 myUtils/app_settings.py），**每次 tick 现读**，
所以改完设置不用重启后端。

调度怎么做的（以及为什么这么简单）
----------------------------------
一个守护线程，每 `TICK_SECONDS`（默认 30 秒）醒一次，看当前 `HH:MM` 是否命中配置里的
时间点；命中就起一个工作线程跑一轮。同一 (日期, 时间点) 只跑一次（`_done_slots`）。

刻意**不做补跑**：进程 00:05 才起来的话，00:00 这个点就跳过了，不会在启动时"补一发"。
4 个点里少一个不影响保活，而补跑会带来"重启一次就满负荷开一轮浏览器"的副作用——
那正是这个项目里其它地方（发布记录、背景音乐状态）都刻意避免的事情。

占用与并发
----------
续期要开浏览器，所以：
  * 走 `browser_session`，和发布中心用**同一把"账号锁"**（key 都是 cookiesFile 下的路径）；
  * 但**不排队**（`wait_timeout=0`）：这个账号正在发布就这一轮先跳过，
    过 `BUSY_RETRY_DELAY`（默认 10 分钟）再补一次，最多补 `BUSY_RETRY_LIMIT` 次。
    排队干等会把 4 个整点变成"整点前后各堵 10 分钟"，比跳过更糟。
  * 整机并发名额由 `browser_session` 统一限流，账号是**顺序**续期的（一次一个），
    免得 0 点时同时开出一堆浏览器把机器拖垮。

状态只放在内存里
----------------
`_last_run` / `_done_slots` 都不落盘。重启后"今天已经跑过的点"不记得了，但也不会重复跑
（只有整点那一分钟内会触发），代价只是重启后会重新显示"还没有续期记录"。
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from conf import BASE_DIR
from myUtils.app_settings import get_session_renew_config
from myUtils.auth import check_cookie
from myUtils.browser_lock import BrowserLockTimeout, browser_session

#: 主循环的检查间隔（秒）。30 秒够用了：最多晚半分钟触发，而一天只有 4 个点。
#: 判断靠的是"当前 HH:MM 是否等于配置的时间点"，所以这个值**必须小于 60**，
#: 否则可能整分钟被跳过、那个点就永远不会触发。
TICK_SECONDS = float(os.environ.get("SAU_SESSION_RENEW_TICK", "30") or 30)

#: 账号正被其它任务占用（发布/校验中）时：多久后补一次、最多补几次。
BUSY_RETRY_DELAY = float(os.environ.get("SAU_SESSION_RENEW_RETRY_DELAY", "600") or 600)
BUSY_RETRY_LIMIT = 2

#: 平台编号 → 名字（user_info.type，和前端 connectSSE 的映射一致）。
PLATFORM_NAMES = {1: "小红书", 2: "视频号", 3: "抖音", 4: "快手"}

#: 续期结果的四种结论。
#:   ok      = 会话有效，续期成功
#:   invalid = 会话已失效（要重新扫码）
#:   busy    = 该账号正在被别的任务用，本轮跳过（会重试）
#:   error   = 打不开页面 / 文件丢了之类的意外
STATUS_OK = "ok"
STATUS_INVALID = "invalid"
STATUS_BUSY = "busy"
STATUS_ERROR = "error"

DB_PATH = Path(BASE_DIR) / "db" / "database.db"
COOKIE_DIR = Path(BASE_DIR) / "cookiesFile"

_state_lock = threading.Lock()
#: 主循环线程（只起一次）
_loop_thread: threading.Thread | None = None
#: 当前有没有一轮续期在跑
_running = False
#: 今天已经跑过的时间点，元素是 (日期, "HH:MM")
_done_slots: set[tuple[str, str]] = set()
#: 因为被占用而没续上的账号：id -> {"attempts": n, "due_at": monotonic 秒}
_pending: dict[int, dict[str, Any]] = {}
#: 最近一轮的结果（给界面显示"上次续期"）
_last_run: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# 账号列表
# --------------------------------------------------------------------------- #
def list_accounts() -> list[dict]:
    """账号管理里的全部账号（所有平台）。

    直接读 user_info：这就是「账号管理」页显示的那张表，续期的范围必须和它一致
    （而不是只看某个平台的 cookies 目录，那样会漏掉刚加进来、还没排过版的账号）。
    """
    if not DB_PATH.is_file():
        return []
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM user_info ORDER BY id").fetchall()
    return [
        {
            "id": int(row["id"]),
            "type": int(row["type"] or 0),
            "file_path": row["filePath"] or "",
            "name": row["userName"] or "",
            "platform": PLATFORM_NAMES.get(int(row["type"] or 0), "未知平台"),
        }
        for row in rows
    ]


def _mark_invalid(account_id: int) -> None:
    """把账号标成失效（status=0），和 `getValidAccounts` 的写法保持一致。

    **只在失败时写**：反方向（校验通过就写 1）看着更"完整"，但各平台的校验都是
    "页面里没出现登录入口就算有效"，页面没加载出来时会误判成有效
    （上游为此专门修过视频号那条）。所以宁可保守——只下不发，
    真恢复了用户点一次刷新就好。
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE user_info SET status = 0 WHERE id = ?", (int(account_id),))
            conn.commit()
    except Exception as exc:  # 数据库被占用之类的意外不该让整轮续期中断
        print(f"⚠️ 会话续期：更新账号 {account_id} 状态失败: {exc}")


# --------------------------------------------------------------------------- #
# 时间点判定（纯函数，方便测试）
# --------------------------------------------------------------------------- #
def plan_due_slots(config: dict, now: datetime, done_slots: Iterable[tuple[str, str]]) -> list[str]:
    """当前时刻该触发哪个时间点（没有就是空列表）。

    只在**整分钟内**命中：所以 TICK_SECONDS 必须小于 60，否则会漏。
    同一 (日期, 时间点) 只返回一次——不然这 60 秒里每 tick 都会再触发一遍。
    """
    if not config.get("enabled"):
        return []
    hhmm = now.strftime("%H:%M")
    if hhmm not in (config.get("times") or []):
        return []
    key = (now.date().isoformat(), hhmm)
    if key in set(done_slots):
        return []
    return [hhmm]


def next_run_at(config: dict, now: datetime | None = None) -> datetime | None:
    """下一次会触发的时间（给界面显示"下次续期"用）；关闭或没配时间点则返回 None。"""
    if not config.get("enabled"):
        return None
    times = config.get("times") or []
    if not times:
        return None
    now = now or datetime.now()
    for value in times:
        hour, minute = (int(x) for x in value.split(":"))
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > now:
            return candidate
    hour, minute = (int(x) for x in times[0].split(":"))
    return (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)


# --------------------------------------------------------------------------- #
# 一个账号的续期
# --------------------------------------------------------------------------- #
def renew_account(account: dict) -> dict:
    """给一个账号续期一次，返回结果行（不抛异常）。"""
    file_name = account.get("file_path") or ""
    account_path = COOKIE_DIR / file_name
    base = {
        "id": account.get("id"),
        "name": account.get("name") or "",
        "platform": account.get("platform") or "",
    }

    if not file_name or not account_path.is_file():
        return {**base, "status": STATUS_ERROR, "message": "cookie 文件不存在，需要重新扫码授权"}

    try:
        # wait_timeout=0：这个账号正在发布/校验就不排队（见模块开头"占用与并发"）。
        with browser_session(account_path, wait_timeout=0):
            ok = asyncio.run(check_cookie(account["type"], file_name, save_state=True))
    except BrowserLockTimeout:
        return {**base, "status": STATUS_BUSY, "message": "该账号正在发布/校验中，本轮跳过"}
    except Exception as exc:
        return {**base, "status": STATUS_ERROR, "message": f"续期出错: {exc}"}

    if ok:
        return {**base, "status": STATUS_OK, "message": "会话有效，已续期"}

    _mark_invalid(account["id"])
    return {**base, "status": STATUS_INVALID, "message": "会话已失效，需要重新扫码授权"}


# --------------------------------------------------------------------------- #
# 一轮续期
# --------------------------------------------------------------------------- #
def run_once(reason: str = "手动", only_ids: Iterable[int] | None = None) -> dict:
    """跑一轮续期（**同步**，顺序处理账号）。定时线程和排查/测试都调它。

    `only_ids` 只跑指定账号（被占用之后的重试用它，不用把整张表再跑一遍）。
    """
    accounts = list_accounts()
    if only_ids is not None:
        wanted = {int(i) for i in only_ids}
        accounts = [a for a in accounts if a["id"] in wanted]

    started = datetime.now()
    print(f"\n🔄 会话续期[{reason}]：{len(accounts)} 个账号，开始逐个校验…")

    rows: list[dict] = []
    for account in accounts:
        row = renew_account(account)
        rows.append(row)
        icon = {
            STATUS_OK: "✅",
            STATUS_INVALID: "⚠️",
            STATUS_BUSY: "🕐",
            STATUS_ERROR: "❌",
        }.get(row.get("status"), "•")
        # 用 get 取名字：一个账号的结果行缺字段不该把整轮续期打断（日志比结论次要）
        label = f"{row.get('platform') or '?'}·{row.get('name') or row.get('id') or '?'}"
        print(f"  {icon} {label}：{row.get('message') or ''}")

    counts = {key: sum(1 for r in rows if r["status"] == key) for key in
              (STATUS_OK, STATUS_INVALID, STATUS_BUSY, STATUS_ERROR)}
    result = {
        "reason": reason,
        "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(rows),
        "ok": counts[STATUS_OK],
        "invalid": counts[STATUS_INVALID],
        "busy": counts[STATUS_BUSY],
        "error": counts[STATUS_ERROR],
        "accounts": rows,
    }
    print(
        f"🔄 会话续期[{reason}]结束：有效 {result['ok']}、失效 {result['invalid']}、"
        f"跳过 {result['busy']}、出错 {result['error']}\n"
    )
    return result


def _remember_run(result: dict) -> None:
    global _last_run
    with _state_lock:
        _last_run = result
        # 被占用的账号排队补一次：不补的话，这个账号今天这一轮就白过了。
        for row in result.get("accounts") or []:
            if row.get("status") != STATUS_BUSY:
                _pending.pop(int(row.get("id") or 0), None)
                continue
            account_id = int(row.get("id") or 0)
            attempts = int((_pending.get(account_id) or {}).get("attempts") or 0) + 1
            if attempts <= BUSY_RETRY_LIMIT:
                _pending[account_id] = {
                    "attempts": attempts,
                    "due_at": time.monotonic() + BUSY_RETRY_DELAY,
                }
            else:
                # 补够了还是被占用（多半是那个账号一直在发东西）：放弃这一轮。
                # 必须真的删掉——留着的话每 tick 都会再捞它出来重试，等于没有上限。
                _pending.pop(account_id, None)
                print(f"⚠️ 会话续期：账号 {row.get('name') or account_id} 连续被占用，本轮不再补跑")


def _worker(reason: str, only_ids: Iterable[int] | None) -> None:
    global _running
    try:
        _remember_run(run_once(reason, only_ids))
    except Exception as exc:  # 兜底：工作线程里的异常不该把调度线程带走
        print(f"⚠️ 会话续期[{reason}]整体失败: {exc}")
    finally:
        with _state_lock:
            _running = False


def _start_run(reason: str, only_ids: Iterable[int] | None = None) -> bool:
    """起一个续期线程。已经有一轮在跑就返回 False（不排队堆积）。"""
    global _running
    with _state_lock:
        if _running:
            return False
        _running = True
    threading.Thread(
        target=_worker, args=(reason, only_ids), name="session-renew", daemon=True
    ).start()
    return True


def _due_retries(now_monotonic: float) -> list[int]:
    """到点该补的账号（顺便把已经没意义的条目清掉）。"""
    with _state_lock:
        ready = [i for i, info in _pending.items() if info["due_at"] <= now_monotonic]
        for account_id in ready:
            _pending.pop(account_id, None)
        return ready


# --------------------------------------------------------------------------- #
# 调度线程
# --------------------------------------------------------------------------- #
def tick(now: datetime | None = None, now_monotonic: float | None = None) -> dict:
    """一次检查：该跑就跑。返回这一 tick 干了什么（排查/测试用）。"""
    now = now or datetime.now()
    now_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
    config = get_session_renew_config()

    with _state_lock:
        # 只留今天的 slot 记录，免得长期运行后这个集合无限长大
        today = now.date().isoformat()
        _done_slots.intersection_update({(today, t) for t in (config.get("times") or [])})
        slots = plan_due_slots(config, now, _done_slots)
        for slot in slots:
            _done_slots.add((today, slot))

    started: list[str] = []
    for slot in slots:
        if _start_run(f"定时 {slot}"):
            started.append(slot)

    retries = _due_retries(now_monotonic)
    if retries and _start_run(f"补跑（{len(retries)} 个账号本轮被占用）", retries):
        started.append("retry")

    return {"slots": slots, "started": started, "retries": retries}


def _loop() -> None:
    while True:
        try:
            tick()
        except Exception as exc:  # 任何意外都不该让调度线程退出（退出了就再也不会续期）
            print(f"⚠️ 会话续期调度出错（下一轮继续）: {exc}")
        time.sleep(TICK_SECONDS)


def start() -> bool:
    """启动调度线程（幂等：重复调用只起一次）。返回是否本次新起了线程。"""
    global _loop_thread
    with _state_lock:
        if _loop_thread is not None and _loop_thread.is_alive():
            return False
        _loop_thread = threading.Thread(target=_loop, name="session-renew-loop", daemon=True)
        _loop_thread.start()
    config = get_session_renew_config()
    state = "开启" if config["enabled"] else "关闭"
    times = "、".join(config["times"]) or "（未配置时间点）"
    print(f"⏰ 账号会话定时续期已启动（当前{state}，时间点 {times}，可在「设置」里改）")
    return True


# --------------------------------------------------------------------------- #
# 给界面看的当前状态
# --------------------------------------------------------------------------- #
def status() -> dict:
    config = get_session_renew_config()
    now = datetime.now()
    upcoming = next_run_at(config, now)
    with _state_lock:
        last_run = _last_run
        running = _running
    return {
        "enabled": bool(config["enabled"]),
        "times": list(config["times"]),
        "running": running,
        "last_run": last_run,
        "next_run": upcoming.strftime("%Y-%m-%d %H:%M") if upcoming else None,
        "next_run_time": upcoming.strftime("%H:%M") if upcoming else None,
    }


__all__ = [
    "PLATFORM_NAMES",
    "STATUS_BUSY",
    "STATUS_ERROR",
    "STATUS_INVALID",
    "STATUS_OK",
    "TICK_SECONDS",
    "list_accounts",
    "next_run_at",
    "plan_due_slots",
    "renew_account",
    "run_once",
    "start",
    "status",
    "tick",
]
