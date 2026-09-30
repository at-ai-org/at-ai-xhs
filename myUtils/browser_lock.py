# -*- coding: utf-8 -*-
"""浏览器操作的并发约束（at-ai-xhs 改造新增）。

两条约束，缺一不可：

1. **同一个账号只能同时开一个浏览器**（`account_lock`）——同账号多会话会互相踢下线；
2. **整机同时开的浏览器数量有上限**（`BROWSER_SLOTS`）——每个 chromium 都是几百 MB，
   不限量的话"并行"只会把机器拖垮，反而更慢。

为什么从"一把全局锁"改成按账号锁
--------------------------------
原来一把全局锁把所有会开浏览器的操作串起来（小红书工作台 + 发布中心），
安全但太慢：不同平台、不同账号之间本来互不相干，却要排队等前一个发完。
现在锁的粒度是**账号 cookie 文件**——同一个文件才会互斥，其余可以并行。

用文件路径（而不是账号名）当 key，是为了让两个入口对**同一个 cookie 文件**也能互斥：
发布中心的账号在 `cookiesFile/`，工作台的账号在 `cookies/`，两边各有一套命名，
只有路径能可靠地判定"是不是同一个账号"。

并发上限用环境变量 `SAU_MAX_CONCURRENT_BROWSERS` 调（默认 4，约等于一个平台一个）。
机器吃不住就调小，机器富裕想再快点就调大。

中断
----
等待的**过程**是可以中途叫醒的：`browser_session(cancel_event=…)` 会在等锁 / 等名额时
以小步轮询的方式回头看这个标志，被中断就抛 `BrowserSessionCancelled`。
不这么做的话，一个排在长队里的任务即使被用户点了中断，也要等满 `LOCK_WAIT_TIMEOUT`
（默认 10 分钟）才轮到它反应——按钮点了跟没点一样。
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

#: 同时最多开几个浏览器。默认 4：常见场景是一个平台一个账号，正好让四个平台并行。
MAX_CONCURRENT_BROWSERS = max(1, int(os.environ.get("SAU_MAX_CONCURRENT_BROWSERS", "4") or 4))

BROWSER_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_BROWSERS)

#: 等「账号锁」或「并发名额」的上限（秒）。超了就抛 `BrowserLockTimeout`，不再无限等。
#:
#: 为什么必须有：线程是杀不掉的。一旦有个任务卡在收尾环节（实测过 `context.close()`
#: 一直 await 一个不再响应的驱动），它会**永久占着**账号锁和并发名额；之后这个账号的
#: 每一次发布都卡在等锁上，既不报错也不超时，界面上就是"一直卡着"。
#: 有了上限，最坏情况变成"几分钟后带着明确原因失败"，用户可以据此重启后端回收。
#:
#: 默认 10 分钟：实测一次正常发布是 1.5~5 分钟，留了一倍余量；
#: 同时**必须小于发布看门狗的 30 分钟**，否则等待中的任务会先被看门狗判死，
#: 这个超时就永远轮不到生效（等于没加）。用 SAU_LOCK_WAIT_TIMEOUT 覆盖（秒）。
LOCK_WAIT_TIMEOUT = float(os.environ.get("SAU_LOCK_WAIT_TIMEOUT") or 10 * 60)

_account_locks: dict[str, threading.RLock] = {}
_registry_lock = threading.Lock()


#: 等锁时多久回来看一眼「有没有被手动中断」。
#:
#: 为什么不能一口气 `acquire(timeout=10min)` 睡到底：那样用户点了中断，
#: 这个任务也要等满 10 分钟才轮到它反应——界面上的中断按钮等于没用。
#: 0.25s 足够让中断感觉是即时的，又不会让轮询本身成为负担
#: （正常情况下一次 acquire 就返回，根本走不到轮询）。
CANCEL_POLL_INTERVAL = 0.25


class BrowserLockTimeout(RuntimeError):
    """等账号锁 / 并发名额超时。

    单独定义成一个类型，是为了让调用方能把"排队排太久"和"页面操作失败"区分开——
    前者该提示用户重启后端回收资源，后者该提示用户查页面/网络。
    """


class BrowserSessionCancelled(RuntimeError):
    """等账号锁 / 并发名额的过程中被手动中断。

    和 `BrowserLockTimeout` 分开，是因为两者对用户的意义完全相反：
    超时是**出事**了（该提示重启后端），中断是**用户要的结果**（该显示「已取消」，
    不该显示成失败）。调用方据此给出不同的结论。
    """


def _wait_text(seconds: float | None = None) -> str:
    """"最多等多久"的文案。默认按全局上限，调用方自定了超时（比如 0 = 不等）就按那个说。"""
    total = LOCK_WAIT_TIMEOUT if seconds is None else float(seconds)
    minutes = total / 60
    return f"{minutes:.0f} 分钟" if minutes >= 1 else f"{total:.0f} 秒"


def _check_cancelled(cancel_event: threading.Event | None) -> None:
    """被中断就抛 `BrowserSessionCancelled`。"""
    if cancel_event is not None and cancel_event.is_set():
        raise BrowserSessionCancelled("任务已被手动中断（浏览器还没开起来，未占用任何资源）")


def _acquire_cancellable(
    primitive, timeout: float, cancel_event: threading.Event | None
) -> bool:
    """可以中途叫醒的 `acquire`。

    返回 True 表示拿到了，False 表示等超时了；被中断则抛 `BrowserSessionCancelled`。

    没有 `cancel_event` 时直接走原语自己的阻塞等待（不改变原有行为）。
    """
    if cancel_event is None:
        return primitive.acquire(timeout=timeout)

    deadline = time.monotonic() + timeout
    while True:
        # 先看中断再看还剩多少时间：用户刚点完中断就能立刻退出，不用等到超时。
        _check_cancelled(cancel_event)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if primitive.acquire(timeout=min(CANCEL_POLL_INTERVAL, remaining)):
            return True


def account_key(*parts: str | Path) -> str:
    """把 cookie 文件路径归一化成锁的 key（绝对路径，同名不同目录不算同一个账号）。"""
    for part in parts:
        text = str(part or "").strip()
        if text:
            return str(Path(text).expanduser().resolve())
    return "<unknown-account>"


def account_lock(key: str | Path) -> threading.RLock:
    """取某个账号的锁。同一个 key 永远拿到同一个锁对象。"""
    safe = account_key(key)
    with _registry_lock:
        lock = _account_locks.get(safe)
        if lock is None:
            lock = threading.RLock()
            _account_locks[safe] = lock
        return lock


@contextmanager
def browser_session(
    key: str | Path,
    on_wait: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    wait_timeout: float | None = None,
):
    """一次浏览器会话：先抢「这个账号」的锁，再抢「整机并发名额」。

    `on_wait(text)` 只在**真的需要排队**时回调一次，用来把"在等什么"告诉用户——
    否则界面上只会看到任务卡着不动，分不清是在跑还是在等。

    两处等待都**有上限**（默认 `LOCK_WAIT_TIMEOUT`）：超时抛 `BrowserLockTimeout`，
    而不是无限期挂着。见该常量的说明。

    `wait_timeout` 是这个上限的按次覆盖（秒）。传 `0` = **完全不等**：给后台的
    自动化任务用（比如账号会话定时续期）——它只是"顺手保活"，碰上这个账号正在发布
    就该让路，而不是把整点堵在这里排队。它只改等待时长，不改别的语义：
    拿到了照样占用锁和名额，用完照样释放。

    `cancel_event` 是可选中断标志（发布中心的「中断」按钮）。传进来之后：
    * 等锁 / 等名额的**过程中**被中断 → 立刻抛 `BrowserSessionCancelled`，
      不会白等满 10 分钟；
    * 名额刚拿到就被中断 → 同样抛出去，浏览器一次都还没开。
    这两种情况下**没有占用任何资源**，锁和名额都会照常释放。
    一个任务正在排长队时，这是中断它代价最小、也最常见的情形。
    """
    timeout = LOCK_WAIT_TIMEOUT if wait_timeout is None else max(0.0, float(wait_timeout))
    lock = account_lock(key)
    if not lock.acquire(blocking=False):
        if on_wait:
            on_wait(f"该账号已有浏览器在跑，最多等 {_wait_text(timeout)}（同账号不能同时开两个）")
        if not _acquire_cancellable(lock, timeout, cancel_event):
            raise BrowserLockTimeout(
                f"等这个账号的浏览器空出来超过 {_wait_text(timeout)}，已放弃。"
                f"常见原因是上一个任务卡死了、它占着锁不释放——重启后端可以立即回收。"
            )
    try:
        if not BROWSER_SLOTS.acquire(blocking=False):
            if on_wait:
                on_wait(f"等一个并发名额（同时最多 {MAX_CONCURRENT_BROWSERS} 个浏览器在跑）")
            if not _acquire_cancellable(BROWSER_SLOTS, timeout, cancel_event):
                raise BrowserLockTimeout(
                    f"等并发名额超过 {_wait_text(timeout)}，已放弃。"
                    f"多半是有任务卡死占着名额不放——重启后端可以立即回收。"
                )
        try:
            # 排队期间可能刚好被中断；此刻浏览器还没开，是中断最干净的时机。
            _check_cancelled(cancel_event)
            yield
        finally:
            BROWSER_SLOTS.release()
    finally:
        lock.release()


__all__ = [
    "MAX_CONCURRENT_BROWSERS",
    "BROWSER_SLOTS",
    "LOCK_WAIT_TIMEOUT",
    "CANCEL_POLL_INTERVAL",
    "BrowserLockTimeout",
    "BrowserSessionCancelled",
    "account_key",
    "account_lock",
    "browser_session",
]
