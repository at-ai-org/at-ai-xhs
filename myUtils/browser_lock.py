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
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

#: 同时最多开几个浏览器。默认 4：常见场景是一个平台一个账号，正好让四个平台并行。
MAX_CONCURRENT_BROWSERS = max(1, int(os.environ.get("SAU_MAX_CONCURRENT_BROWSERS", "4") or 4))

BROWSER_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_BROWSERS)

_account_locks: dict[str, threading.RLock] = {}
_registry_lock = threading.Lock()


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
def browser_session(key: str | Path, on_wait: Callable[[str], None] | None = None):
    """一次浏览器会话：先抢「这个账号」的锁，再抢「整机并发名额」。

    `on_wait(text)` 只在**真的需要排队**时回调一次，用来把"在等什么"告诉用户——
    否则界面上只会看到任务卡着不动，分不清是在跑还是在等。
    """
    lock = account_lock(key)
    if not lock.acquire(blocking=False):
        if on_wait:
            on_wait("该账号已有浏览器在跑，等它跑完（同账号不能同时开两个）")
        lock.acquire()
    try:
        if not BROWSER_SLOTS.acquire(blocking=False):
            if on_wait:
                on_wait(f"等一个并发名额（同时最多 {MAX_CONCURRENT_BROWSERS} 个浏览器在跑）")
            BROWSER_SLOTS.acquire()
        try:
            yield
        finally:
            BROWSER_SLOTS.release()
    finally:
        lock.release()


__all__ = [
    "MAX_CONCURRENT_BROWSERS",
    "BROWSER_SLOTS",
    "account_key",
    "account_lock",
    "browser_session",
]
