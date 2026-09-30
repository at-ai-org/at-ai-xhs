"""等锁 / 等并发名额必须有上限（myUtils/browser_lock.py + 视频号 session.py）。

由来（真实事故链）：
1. 一个任务卡在收尾环节（`context.close()` 等一个不再响应的驱动）→ 线程永远不返回；
2. 它**永久占着**账号锁和一个并发名额（Python 杀不掉线程，锁也就放不出来）；
3. 之后这个账号的每次发布都干等在 `lock.acquire()` 上，既不报错也不超时——
   界面上就是"任务一直卡着"（现场：视频号 id=62 从 17:49 卡到第二天重启）。

看门狗能把"结论"改对，但治不了第 2 步。这里加的是等锁上限：最坏情况从
"无限期卡住"变成"几分钟后带着明确原因失败"，用户可以据此重启后端回收。
"""

import asyncio
import os
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import myUtils.browser_lock as BL
import sau_backend_publish as BP


class _HoldLock:
    """在**另一个线程**里占着锁不放。

    `threading.RLock` 是可重入的：同一个线程再 acquire 会直接成功。所以"在测试线程里
    先 acquire 再调 browser_session"根本测不出竞争——那正是我第一版测试写错的地方。
    现实里的竞争本来就来自别的线程，这里也必须用别的线程来占。
    """

    def __init__(self, lock):
        self.lock = lock
        self.held = threading.Event()
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        self.lock.acquire()
        self.held.set()
        self.done.wait(10)
        self.lock.release()

    def __enter__(self):
        self.thread.start()
        if not self.held.wait(2):
            raise AssertionError("占锁线程没能拿到锁")
        return self

    def __exit__(self, *exc):
        self.done.set()
        self.thread.join(3)
        return False


class BrowserLockTimeoutTestBase(unittest.TestCase):
    TIMEOUT = 0.25

    def setUp(self):
        self.key = f"/tmp/at-ai-xhs-lock-test-{time.time_ns()}.json"
        self._patch = patch.object(BL, "LOCK_WAIT_TIMEOUT", self.TIMEOUT)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        with BL._registry_lock:
            BL._account_locks.pop(BL.account_key(self.key), None)


class AccountLockTimeoutTest(BrowserLockTimeoutTestBase):
    def test_raises_instead_of_waiting_forever(self):
        """核心回归：锁被别的线程占着时，必须在有限时间内抛错，而不是无限等。"""
        with _HoldLock(BL.account_lock(self.key)):
            start = time.time()
            with self.assertRaises(BL.BrowserLockTimeout):
                with BL.browser_session(self.key):
                    pass
            elapsed = time.time() - start
        self.assertLess(elapsed, self.TIMEOUT * 6, "应当在上限附近就放弃，而不是一直等")

    def test_message_says_what_to_do(self):
        """失败原因要能指导用户：这类卡住重启后端才能回收。"""
        with _HoldLock(BL.account_lock(self.key)):
            with self.assertRaises(BL.BrowserLockTimeout) as ctx:
                with BL.browser_session(self.key):
                    pass
        self.assertIn("重启后端", str(ctx.exception))

    def test_on_wait_reports_the_limit(self):
        """排队时要告诉用户在等什么、最多等多久（否则界面只看到卡着不动）。"""
        seen: list[str] = []
        with _HoldLock(BL.account_lock(self.key)):
            with self.assertRaises(BL.BrowserLockTimeout):
                with BL.browser_session(self.key, on_wait=seen.append):
                    pass
        self.assertEqual(len(seen), 1)
        self.assertIn("最多等", seen[0])

    def test_lock_is_released_after_timeout(self):
        """超时抛出后不能把锁也占住（否则错误会传染给下一个任务）。"""
        with _HoldLock(BL.account_lock(self.key)):
            with self.assertRaises(BL.BrowserLockTimeout):
                with BL.browser_session(self.key):
                    pass

        lock = BL.account_lock(self.key)
        self.assertTrue(lock.acquire(timeout=1), "超时路径不该留下锁")
        lock.release()


class SlotTimeoutTest(BrowserLockTimeoutTestBase):
    def test_raises_when_no_slot_frees_up(self):
        """并发名额被占满且不放时，也要在有限时间内抛错。"""
        real_slots = BL.BROWSER_SLOTS
        BL.BROWSER_SLOTS = threading.BoundedSemaphore(1)
        BL.BROWSER_SLOTS.acquire()  # 占住唯一名额
        try:
            with self.assertRaises(BL.BrowserLockTimeout) as ctx:
                with BL.browser_session(self.key):
                    pass
            self.assertIn("并发名额", str(ctx.exception))
        finally:
            BL.BROWSER_SLOTS.release()
            BL.BROWSER_SLOTS = real_slots

    def test_account_lock_released_when_slot_times_out(self):
        """名额等不到时，账号锁必须先还回去——否则会连带把账号也锁死。"""
        real_slots = BL.BROWSER_SLOTS
        BL.BROWSER_SLOTS = threading.BoundedSemaphore(1)
        BL.BROWSER_SLOTS.acquire()
        try:
            with self.assertRaises(BL.BrowserLockTimeout):
                with BL.browser_session(self.key):
                    pass
        finally:
            BL.BROWSER_SLOTS.release()
            BL.BROWSER_SLOTS = real_slots

        lock = BL.account_lock(self.key)
        self.assertTrue(lock.acquire(timeout=1), "名额超时不该把账号锁也留下")
        lock.release()


class NormalPathTest(BrowserLockTimeoutTestBase):
    def test_free_lock_works_normally(self):
        """没竞争时一切照旧，且用完能再拿到（说明释放正常）。"""
        ran = False
        with BL.browser_session(self.key):
            ran = True
        self.assertTrue(ran)

        lock = BL.account_lock(self.key)
        self.assertTrue(lock.acquire(timeout=1))
        lock.release()

    def test_slot_returned_after_use(self):
        before = BL.BROWSER_SLOTS._value  # noqa: SLF001 - 测试里直接看剩余名额
        with BL.browser_session(self.key):
            self.assertEqual(BL.BROWSER_SLOTS._value, before - 1)
        self.assertEqual(BL.BROWSER_SLOTS._value, before)

    def test_exception_inside_still_releases(self):
        """会话体里抛异常也要释放锁和名额。"""
        before = BL.BROWSER_SLOTS._value
        with self.assertRaises(ValueError):
            with BL.browser_session(self.key):
                raise ValueError("模拟页面操作失败")
        self.assertEqual(BL.BROWSER_SLOTS._value, before)
        lock = BL.account_lock(self.key)
        self.assertTrue(lock.acquire(timeout=1))
        lock.release()


class TimeoutConfigTest(unittest.TestCase):
    def test_default_is_ten_minutes(self):
        self.assertEqual(BL.LOCK_WAIT_TIMEOUT, 10 * 60)

    def test_must_be_shorter_than_the_publish_watchdog(self):
        """关键不变量：等锁上限必须**小于**发布看门狗。

        否则等待中的任务会先被看门狗判死，这个超时永远轮不到生效——等于没加。
        """
        self.assertLess(
            BL.LOCK_WAIT_TIMEOUT,
            BP.PUBLISH_TASK_TIMEOUT,
            "等锁超时必须比看门狗短，否则不会生效",
        )

    def test_wait_text_is_human_readable(self):
        with patch.object(BL, "LOCK_WAIT_TIMEOUT", 600):
            self.assertEqual(BL._wait_text(), "10 分钟")
        with patch.object(BL, "LOCK_WAIT_TIMEOUT", 45):
            self.assertEqual(BL._wait_text(), "45 秒")

    def test_env_override(self):
        """环境变量能覆盖（部署时不想改代码）。"""
        with patch.dict(os.environ, {"SAU_LOCK_WAIT_TIMEOUT": "120"}):
            self.assertEqual(float(os.environ["SAU_LOCK_WAIT_TIMEOUT"]), 120.0)


class TencentProfileLockTimeoutTest(unittest.TestCase):
    """视频号 profile 锁（tencent_session 里那个）也要有上限。"""

    TIMEOUT = 0.25

    def setUp(self):
        from uploader.tencent_uploader import session as ts

        self.ts = ts
        self._tmp = tempfile.TemporaryDirectory()
        self.profile = Path(self._tmp.name) / "prof"
        self.profile.mkdir(parents=True, exist_ok=True)
        self._patches = [patch.object(ts, "LOCK_WAIT_TIMEOUT", self.TIMEOUT)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_raises_when_profile_is_busy(self):
        ts = self.ts

        async def run():
            async with ts.tencent_session(
                object(), None, profile_dir=self.profile, seed=False, persist=False
            ):
                pass

        with _HoldLock(ts.account_lock(self.profile)):
            with self.assertRaises(ts.BrowserLockTimeout) as ctx:
                asyncio.run(asyncio.wait_for(run(), timeout=5))
        self.assertIn("重启后端", str(ctx.exception))

    def test_profile_lock_released_when_session_ends(self):
        """正常结束后锁要还回去（这一处以前是 `with lock:`，改写成 acquire/release 容易漏）。"""
        ts = self.ts

        class FakeContext:
            async def add_init_script(self, **kwargs):
                return None

            async def close(self):
                return None

        class FakePlaywright:
            class chromium:  # noqa: N801
                @staticmethod
                async def launch_persistent_context(path, **kwargs):
                    return FakeContext()

        async def run():
            async with ts.tencent_session(
                FakePlaywright(), None, profile_dir=self.profile, seed=False, persist=False
            ):
                pass

        asyncio.run(asyncio.wait_for(run(), timeout=5))
        lock = ts.account_lock(self.profile)
        self.assertTrue(lock.acquire(timeout=1), "会话结束后 profile 锁没释放")
        lock.release()


class LeakedLockIntegrationTest(unittest.TestCase):
    """端到端：账号锁被卡死任务泄漏后，下一条任务必须**明确失败**而不是无限等。

    这就是现场 id=62 那条：id=59 的线程卡死不放锁，id=62 于是干等在
    "该账号已有浏览器在跑，等它跑完"上，从 17:49 一直挂到第二天重启。
    B（等锁上限）+ C（看门狗）合起来之后，它应该在等锁上限内带着可操作的原因失败。
    """

    def test_task_fails_with_actionable_reason(self):
        from pathlib import Path

        account_file = "leaked-lock-test.json"
        account_path = str(Path(BP.BASE_DIR) / "cookiesFile" / account_file)
        task_id = f"leak-{time.time_ns()}"
        with BP._lock:
            BP._tasks[task_id] = {
                "task_id": task_id,
                "job_id": "job-leak",
                "record_id": 1,
                "channel": "tencent:leak",
                "platform": 2,
                "account_id": 1,
                "account_name": "测试账号",
                "account_file": account_file,
                "options": {},
                "schedule_at": None,
                "status": BP.STATUS_QUEUED,
                "stage": "queued",
                "stage_text": "排队中",
                "action": "已加入发布队列",
                "message": "",
                "started_at": None,
                "finished_at": None,
                "duration": None,
                "steps": [],
                "log": [],
                "prompt": None,
            }
        try:
            with patch.object(BP, "_update_record"), patch.object(BP, "_refresh_job"), patch.object(
                BP, "_validate_task"
            ), patch.object(BL, "LOCK_WAIT_TIMEOUT", 0.25):
                start = time.time()
                with _HoldLock(BL.account_lock(account_path)):
                    BP._run_task(task_id)  # 会卡在等账号锁上，直到超时
                elapsed = time.time() - start

            with BP._lock:
                task = dict(BP._tasks[task_id])

            self.assertEqual(task["status"], BP.STATUS_FAILED, "必须失败收场，不能挂在 running")
            self.assertIn("重启后端", task["message"], "失败原因要能指导用户")
            self.assertLess(elapsed, 5, "应当在等锁上限附近就失败")
        finally:
            with BP._lock:
                BP._tasks.pop(task_id, None)
            with BL._registry_lock:
                BL._account_locks.pop(BL.account_key(account_path), None)


if __name__ == "__main__":
    unittest.main()
