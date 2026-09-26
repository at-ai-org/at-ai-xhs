# Language: 中文
"""发布途中「要人搭把手」输入桥的回归测试（`myUtils/publish_prompt.py`）。

这条链路的价值全在"**发布线程真的被叫醒了**"这一点上：
抖音短信验证码填不进去，整条发布就是白跑。所以这里重点验证三件事：
有人填 → 值能传回上传器；用户跳过 / 等超时 → 上传器拿到 None 且不卡死；
请求收尾时一定会通知执行器撤下弹窗（否则界面上会挂着一个永远填不完的框）。
"""

import asyncio
import threading
import time
import unittest

import myUtils.publish_prompt as prompts


def _wait_for_pending(timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = prompts.pending_requests()
        if pending:
            return pending[0]
        time.sleep(0.02)
    raise AssertionError("3 秒内没有出现等待中的输入请求")


class PublishPromptTests(unittest.TestCase):
    def setUp(self):
        self.events: list[dict] = []

    def _run_in_thread(self, coro_factory, results: list):
        def runner():
            prompts.bind(self.events.append)
            try:
                results.append(asyncio.run(coro_factory()))
            finally:
                prompts.unbind()

        thread = threading.Thread(target=runner)
        thread.start()
        return thread

    def test_answer_is_delivered_to_waiting_uploader(self):
        results = []
        thread = self._run_in_thread(
            lambda: prompts.request_input(prompts.KIND_SMS_CODE, title="t", message="m", timeout=5),
            results,
        )
        pending = _wait_for_pending()

        self.assertTrue(prompts.answer(pending["prompt_id"], " 123456 "))
        thread.join(timeout=5)

        self.assertEqual(results, ["123456"])
        # 开、关都要通知执行器：只开不关的话，界面上会留一个填不完的弹窗
        self.assertEqual([e["type"] for e in self.events], ["prompt_open", "prompt_close"])
        self.assertEqual(self.events[0]["prompt"]["kind"], prompts.KIND_SMS_CODE)
        self.assertEqual(self.events[1]["prompt_id"], pending["prompt_id"])
        # 收尾之后不该再留在注册表里
        self.assertEqual(prompts.pending_requests(), [])

    def test_skip_returns_none_and_releases_uploader(self):
        results = []
        thread = self._run_in_thread(
            lambda: prompts.request_input(prompts.KIND_SMS_CODE, message="m", timeout=5),
            results,
        )
        pending = _wait_for_pending()

        self.assertTrue(prompts.skip(pending["prompt_id"]))
        thread.join(timeout=5)

        self.assertEqual(results, [None])

    def test_timeout_returns_none_instead_of_hanging(self):
        prompts.bind(self.events.append)
        try:
            started = time.time()
            value = asyncio.run(prompts.request_input(prompts.KIND_SMS_CODE, message="m", timeout=0.3))
        finally:
            prompts.unbind()

        self.assertIsNone(value)
        self.assertLess(time.time() - started, 3)

    def test_late_or_unknown_answer_is_ignored(self):
        self.assertFalse(prompts.answer("not-a-real-prompt-id", "123456"))
        self.assertFalse(prompts.skip("not-a-real-prompt-id"))

    def test_without_bridge_falls_back_to_no_input(self):
        """没有界面（CLI/测试）且 stdin 不是终端时，必须立刻返回而不是干等。"""
        started = time.time()
        value = asyncio.run(prompts.request_input(prompts.KIND_SMS_CODE, message="m", timeout=0.3))
        self.assertIsNone(value)
        self.assertLess(time.time() - started, 3)


if __name__ == "__main__":
    unittest.main()
