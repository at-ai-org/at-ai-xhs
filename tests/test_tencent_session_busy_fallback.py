# -*- coding: utf-8 -*-
"""`tencent_session(fallback_when_busy=True)` 抢 profile 锁必须**只抢一次**。

由来（2026-09-29 真实事故）
--------------------------
`uploader/tencent_uploader/session.py` 里曾经是这么写的：

    lock = account_lock(target)
    if fallback_when_busy and not lock.acquire(blocking=False):   # ← 锁空闲时这次就拿到了
        ... 只读降级 ...
        return
    if not lock.acquire(timeout=LOCK_WAIT_TIMEOUT):               # ← 又抢一次，计数变成 2
        raise BrowserLockTimeout(...)
    try:
        ...
    finally:
        lock.release()                                            # ← 只还一次，计数停在 1

`acquire(blocking=False)` **不是"探测"**：成功时它真的把锁拿走了。于是只要锁空闲、
又带着 `fallback_when_busy=True`（视频号 `cookie_auth` 的唯一调用方式：账号页校验、
登录后校验、每次发布的上传前检查都走它），每调用一次就永久多留一层锁计数。

`RLock` 是**同线程可重入、跨线程互斥**的，症状因此格外有迷惑性：

* 泄漏它的那条任务线程自己还能再进去 —— 于是"重启后端后第一条视频号发布照样成功"；
* 任务线程退出后，这把锁就被一个**已经不存在的线程**占着，其它线程永远拿到 `False`：
  日志永远打 `🕐 正被其它任务占用（多半在发布）`，真正的发布会卡满 `LOCK_WAIT_TIMEOUT`
  （默认 10 分钟）才失败，**只有重启后端能回收**。这就是"重启完能发一次、下次又不行"。

为什么以前的测试没抓到
----------------------
`tests/test_browser_lock_timeout.py::test_profile_lock_released_when_session_ends`
走的是默认 `fallback_when_busy=False`，从来不进上面那个分支。

两个测试写法上的坑（都踩过，所以这里特意规避）
---------------------------------------------
1. **不能用"持有锁的那条线程"去试探**：`RLock` 可重入，同线程再 acquire 一定会成功，
   泄漏会被完全藏起来。所以本文件一律从**另一条线程**试探。
2. **不能让"泄漏锁的线程"先退出再去试探**：Python 的线程标识会被复用，新线程很可能
   拿到同一个标识，于是复现变成抽奖（线上表现为"偶尔能多发几条"）。
   所以这里让泄漏的那条线程**保持存活**（`keep_alive`）直到断言结束——两条活着的线程
   标识必定不同，复现是确定性的。

本文件不启动任何浏览器（假 playwright / 假 context），跑得很快。
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from uploader.tencent_uploader import session as ts


# --------------------------------------------------------------------------- #
# 假 playwright：够 case 走完两条路径（持久化 profile / 一次性 context）
# --------------------------------------------------------------------------- #
class FakeContext:
    def __init__(self) -> None:
        self.closed = False

    async def add_init_script(self, **kwargs):
        return None

    async def cookies(self):
        return [{"name": "sessionid", "value": "x"}]

    async def storage_state(self, path=None):
        return None

    async def close(self):
        self.closed = True


class FakeBrowser:
    """一次性 context 那条路（`chromium.launch` + `browser.new_context`）。"""

    def __init__(self) -> None:
        self.contexts: list[FakeContext] = []
        self.closed = False

    async def new_context(self, **kwargs):
        context = FakeContext()
        self.contexts.append(context)
        return context

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self) -> None:
        self.persistent_calls: list[str] = []
        self.ephemeral_calls: list[dict] = []
        self.persistent_context = FakeContext()
        self.ephemeral_browser = FakeBrowser()

    async def launch_persistent_context(self, path, **kwargs):
        self.persistent_calls.append(str(path))
        return self.persistent_context

    async def launch(self, **kwargs):
        self.ephemeral_calls.append(kwargs)
        return self.ephemeral_browser


class FakePlaywright:
    def __init__(self) -> None:
        self.chromium = FakeChromium()


class RecordingLogger:
    """替掉真 logger：既不往 logs/tencent.log 里灌测试噪音，又能断言打了什么。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def _record(self, level):
        def _sink(text, *args, **kwargs):
            self.lines.append(f"{level} {text}")
        return _sink

    def __getattr__(self, level):
        return self._record(level)

    def joined(self) -> str:
        return "\n".join(self.lines)


class SessionThread:
    """一条"任务线程"：在里面开一次会话，可以选择会话结束后继续活着。"""

    def __init__(self, playwright, profile: Path, *, keep_alive: bool, **kwargs) -> None:
        self.seen: dict = {}
        self.error: BaseException | None = None
        self._release = threading.Event()
        self._done = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(playwright, profile, keep_alive, kwargs),
            name="publish-task-test", daemon=True,
        )

    def _run(self, playwright, profile, keep_alive, kwargs) -> None:
        async def body() -> None:
            async with ts.tencent_session(playwright, None, profile_dir=profile, **kwargs) as session:
                self.seen["ephemeral"] = session.ephemeral
                self.seen["profile_dir"] = session.profile_dir

        try:
            asyncio.run(asyncio.wait_for(body(), timeout=5))
        except BaseException as exc:  # noqa: BLE001 - 原样带回主线程断言
            self.error = exc
        finally:
            self._done.set()
        if keep_alive:
            # 故意不退出：让这条线程的标识一直被占着，另一个线程的试探才有确定性
            self._release.wait(timeout=10)

    def start(self) -> "SessionThread":
        self._thread.start()
        return self

    def wait_done(self, timeout: float = 10) -> bool:
        """等会话跑完（不等于线程退出：keep_alive 时会话结束后线程还留着）。"""
        return self._done.wait(timeout=timeout)

    def finish(self) -> None:
        """放这条线程走（它只是被有意留着的）。"""
        self._release.set()
        self._thread.join(timeout=10)


class BusyFallbackLockTestBase(unittest.TestCase):
    TIMEOUT = 0.25          # 等锁上限压到 0.25s，免得跑得慢

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.profile = self.root / "uin_3322075105"
        self.logger = RecordingLogger()
        self._threads: list[SessionThread] = []
        self._patches = [
            patch.object(ts, "TENCENT_PROFILE_ROOT", self.root),
            patch.object(ts, "LOCK_WAIT_TIMEOUT", self.TIMEOUT),
            patch.object(ts, "tencent_logger", self.logger),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self) -> None:
        for thread in self._threads:
            thread.finish()
        for item in self._patches:
            item.stop()
        self._tmp.cleanup()

    # -- 工具 ------------------------------------------------------------- #
    def run_session(self, *, keep_alive: bool = False, **kwargs) -> SessionThread:
        """按生产的方式开一次会话：在**子线程**里（每个任务一条新线程）。"""
        thread = SessionThread(FakePlaywright(), self.profile, keep_alive=keep_alive, **kwargs)
        self._threads.append(thread)
        thread.start()
        self.assertTrue(thread.wait_done(), "会话线程没有在 10 秒内跑完")
        return thread

    def fresh_thread_can_acquire_lock(self) -> bool:
        """从一条**新开的、与其它线程共存**的线程试探这把锁（等价于"下一个任务来抢"）。"""
        box: dict = {}

        def probe() -> None:
            lock = ts.account_lock(self.profile)
            got = lock.acquire(blocking=False)
            if got:
                lock.release()
            box["got"] = got

        thread = threading.Thread(target=probe, name="fresh-probe", daemon=True)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "试探线程卡住了")
        return box["got"]

    def hold_lock_as_another_task(self):
        """在主线程占住这把锁，模拟"同账号已有任务在跑"。"""
        lock = ts.account_lock(self.profile)
        self.assertTrue(lock.acquire(blocking=False), "测试自身没能占住锁")
        return lock


class FallbackEnabledLockReleaseTest(BusyFallbackLockTestBase):
    """回归：`fallback_when_busy=True` + 锁空闲 → 会话结束后锁必须还回来。"""

    def test_lock_is_released_when_lock_was_free(self):
        thread = self.run_session(
            keep_alive=True, seed=False, persist=False, fallback_when_busy=True
        )
        self.assertIsNone(thread.error)
        # 锁空闲时走的是持久化 profile 那条路，不该降级
        self.assertFalse(thread.seen["ephemeral"], "锁空闲时不该降级成一次性 context")
        self.assertTrue(
            self.fresh_thread_can_acquire_lock(),
            "会话结束后 profile 锁没释放：同账号下一个任务（另一条线程）会永远拿到 False，"
            "只能打 🕐 并等锁超时，直到重启后端——这就是 2026-09-29 的线上事故",
        )


class SecondTaskInNewThreadTest(BusyFallbackLockTestBase):
    """用户的原始症状：重启后第一条视频号能发，下一次就不行。

    生产里同一账号的每个任务都是**新的子线程**，所以这里连开两次会话，
    第二次仍然要能拿到（持久化）锁、不许降级。
    """

    def test_second_task_does_not_degrade(self):
        first = self.run_session(
            keep_alive=True, seed=False, persist=False, fallback_when_busy=True
        )
        self.assertIsNone(first.error)
        self.assertFalse(first.seen["ephemeral"], "第一条任务自己就降级了，说明测试前提不对")

        # 第一条的线程还活着（标识被占住），所以第二条一定拿到不同的标识 —— 确定性复现
        second = self.run_session(
            keep_alive=True, seed=False, persist=False, fallback_when_busy=True
        )
        self.assertIsNone(second.error)
        self.assertFalse(
            second.seen["ephemeral"],
            "第二个任务降级成只读探测了：说明第一个任务把 profile 锁泄漏了，"
            "它接下来真正的发布步骤会卡满等锁上限然后失败",
        )
        self.assertNotIn("正被其它任务占用", self.logger.joined(), "不该出现 🕐 降级提示")


class BusyLockDegradeTest(BusyFallbackLockTestBase):
    """锁真的被别人占着时，降级仍然是**正确且必须保留**的行为。"""

    def test_busy_lock_degrades_to_ephemeral_probe(self):
        holder = self.hold_lock_as_another_task()
        try:
            thread = self.run_session(
                keep_alive=True, seed=False, persist=False, fallback_when_busy=True
            )
            self.assertIsNone(thread.error)
            self.assertTrue(thread.seen["ephemeral"], "锁被占着时应该降级成一次性 context")
            self.assertIn("正被其它任务占用", self.logger.joined())
            # 降级不该动别人的锁（持有线程还活着，所以这里的探试是可信的）
            self.assertFalse(
                self.fresh_thread_can_acquire_lock(), "降级路径动了别人的锁（更糟）"
            )
        finally:
            holder.release()

    def test_busy_lock_without_fallback_still_times_out(self):
        """`fallback_when_busy=False`（发布那条路）仍然要有上限地失败，不许无限等。"""
        holder = self.hold_lock_as_another_task()
        try:
            thread = self.run_session(seed=False, persist=False)
        finally:
            holder.release()

        self.assertIsInstance(thread.error, ts.BrowserLockTimeout)
        self.assertIn("重启后端", str(thread.error))


if __name__ == "__main__":
    unittest.main()
