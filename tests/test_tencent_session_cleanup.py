"""视频号会话的**收尾必须有界**（uploader/tencent_uploader/session.py）。

由来（真实事故）：`profile 锁` 的持有范围跨越了收尾动作——

    with lock:                      # 拿锁
        context = await launch_persistent_context(...)
        try:
            yield ...
        finally:
            await save_storage_state(...)   # 无超时
            await context.close()           # 无超时
                                            # 直到这里才放锁

实测场景：Chromium 内核已经没了、但 patchright 驱动进程还活着，`context.close()`
就一直 await 下去 → **锁永远不释放** → 之后这个账号的发布任务全部卡在等锁上，
既不报错也不超时（界面上就是"任务一直卡着"）。这里锁定的就是"收尾超时也一定要放锁"。
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from uploader.tencent_uploader import session as ts


class FakeContext:
    """可控的 context 替身：能模拟 close() / storage_state() 永久挂住。"""

    def __init__(self, *, hang_close: bool = False, hang_storage: bool = False, cookies=None):
        self.hang_close = hang_close
        self.hang_storage = hang_storage
        self._cookies = cookies if cookies is not None else [{"name": "sessionid", "value": "x"}]
        self.closed = False
        self.storage_written = False

    async def add_init_script(self, **kwargs):
        return None

    async def cookies(self):
        return self._cookies

    async def storage_state(self, path=None):
        if self.hang_storage:
            await asyncio.Event().wait()  # 永不返回
        self.storage_written = True

    async def close(self):
        if self.hang_close:
            await asyncio.Event().wait()  # 永不返回
        self.closed = True


class FakeChromium:
    def __init__(self, context):
        self._context = context
        self.launch_calls = []

    async def launch_persistent_context(self, path, **kwargs):
        self.launch_calls.append({"path": path, **kwargs})
        return self._context


class FakePlaywright:
    def __init__(self, context):
        self.chromium = FakeChromium(context)


class CleanupTimeoutTestBase(unittest.TestCase):
    #: 测试里把超时压到很小，免得跑得慢
    TIMEOUT = 0.2

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._patches = [
            patch.object(ts, "CLEANUP_TIMEOUT", self.TIMEOUT),
            patch.object(ts, "TENCENT_PROFILE_ROOT", self.root),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _run_session(self, context, *, persist=True):
        """跑一次会话；外层再套一个硬超时，没修好的话会在这里超时失败。"""
        captured = {}

        async def run():
            account_file = self.root / "acc.json"
            async with ts.tencent_session(
                FakePlaywright(context),
                account_file if persist else None,
                profile_dir=self.root / "prof",
                seed=False,
                persist=persist,
            ) as session:
                captured["profile_dir"] = session.profile_dir
                captured["next"] = True  # 走到 yield 之后才算会话真的建立了

        asyncio.run(asyncio.wait_for(run(), timeout=10))
        return captured

    def _assert_lock_released(self, profile_dir):
        """锁必须已经释放——这是整件事的关键。"""
        lock = ts.account_lock(profile_dir)
        self.assertTrue(
            lock.acquire(timeout=2),
            "收尾超时后 profile 锁没有释放；后续发布任务会永远卡在等锁上",
        )
        lock.release()


class CloseTimeoutTest(CleanupTimeoutTestBase):
    def test_close_that_never_returns_does_not_hang_the_session(self):
        context = FakeContext(hang_close=True)
        captured = self._run_session(context, persist=False)
        self.assertTrue(captured["next"], "会话体应当正常执行完")

    def test_lock_is_released_even_when_close_hangs(self):
        context = FakeContext(hang_close=True)
        captured = self._run_session(context, persist=False)
        self._assert_lock_released(captured["profile_dir"])

    def test_normal_close_still_works(self):
        """加了超时不能影响正常路径：该关的还是要关。"""
        context = FakeContext()
        self._run_session(context, persist=False)
        self.assertTrue(context.closed)


class StorageTimeoutTest(CleanupTimeoutTestBase):
    def test_storage_state_that_never_returns_does_not_hang_the_session(self):
        context = FakeContext(hang_storage=True)
        captured = self._run_session(context)
        self.assertTrue(captured["next"])

    def test_lock_is_released_even_when_storage_write_hangs(self):
        """回写 cookie 挂住也不能占着锁——回写是"尽力而为"，锁是硬约束。"""
        context = FakeContext(hang_storage=True)
        captured = self._run_session(context)
        self._assert_lock_released(captured["profile_dir"])

    def test_normal_storage_write_still_happens(self):
        context = FakeContext()
        self._run_session(context)
        self.assertTrue(context.storage_written, "正常路径下 cookie 仍要回写")

    def test_close_still_attempted_after_storage_timeout(self):
        """回写超时后仍要继续关浏览器——不能因为一步失败就跳过余下的收尾。"""
        context = FakeContext(hang_storage=True)
        self._run_session(context)
        self.assertTrue(context.closed)


class TimeoutConfigTest(unittest.TestCase):
    def test_timeout_is_a_sane_positive_number(self):
        self.assertGreater(ts.CLEANUP_TIMEOUT, 0)
        self.assertLess(ts.CLEANUP_TIMEOUT, 10 * 60, "收尾超时不该长到把发布拖住")

    def test_timeout_is_exported(self):
        self.assertIn("CLEANUP_TIMEOUT", ts.__all__)


if __name__ == "__main__":
    unittest.main()
