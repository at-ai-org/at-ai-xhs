# Language: 中文
"""抖音短信验证码那条路的回归测试。

用户的要求很明确：**发布时如果有短信，直接弹窗让人填，不要去检查那个文件**。
所以这里锁两件事：

1. "写个 verify_code.txt 等程序来捡"那条路必须彻底不存在了
   （它用的人既看不见也猜不到，出问题时连查都没法查）；
2. 弹窗请求真的能走通：`_ask_sms_code()` 通过 `publish_prompt` 把值拿回来；
3. 只有**确实像**短信验证的输入框才触发弹窗——页面上别的 `type=tel` 输入框
   不该让发布线程干等 5 分钟。
"""

import asyncio
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from uploader.douyin_uploader import main as douyin_main
from uploader.douyin_uploader.main import DouYinVideo
import myUtils.publish_prompt as prompts


def _video() -> DouYinVideo:
    return DouYinVideo("标题", "/tmp/demo.mp4", [], 0, "/tmp/cookie.json")


def _page_with_get_code(count: int):
    page = MagicMock()
    locator = MagicMock()
    locator.first = locator
    locator.count = AsyncMock(return_value=count)
    page.get_by_text.return_value = locator
    return page


def _sms_input(placeholder):
    sms_input = MagicMock()
    if isinstance(placeholder, Exception):
        sms_input.get_attribute = AsyncMock(side_effect=placeholder)
    else:
        sms_input.get_attribute = AsyncMock(return_value=placeholder)
    return sms_input


class DouyinSmsPromptTests(unittest.TestCase):
    def test_file_based_verify_code_is_gone(self):
        source = Path(douyin_main.__file__).read_text(encoding="utf-8")
        self.assertNotIn("_read_verify_code", source)
        self.assertNotIn('"verify_code.txt"', source)

    def test_ask_sms_code_goes_through_the_prompt_bridge(self):
        results = []

        def runner():
            prompts.bind(lambda event: None)
            try:
                results.append(asyncio.run(douyin_main._ask_sms_code()))
            finally:
                prompts.unbind()

        thread = threading.Thread(target=runner)
        thread.start()
        deadline = time.time() + 3
        pending = []
        while time.time() < deadline:
            pending = prompts.pending_requests()
            if pending:
                break
            time.sleep(0.02)
        self.assertTrue(pending, "没有把弹窗请求发出来")
        self.assertEqual(pending[0]["kind"], prompts.KIND_SMS_CODE)
        self.assertTrue(prompts.answer(pending[0]["prompt_id"], " 135790 "))
        thread.join(timeout=5)
        self.assertEqual(results, ["135790"])

    def test_placeholder_with_sms_wording_counts(self):
        video = _video()
        page = _page_with_get_code(0)
        self.assertTrue(
            asyncio.run(video._looks_like_sms_dialog(page, _sms_input("请输入验证码")))
        )

    def test_get_code_button_counts_even_without_placeholder(self):
        video = _video()
        page = _page_with_get_code(1)
        self.assertTrue(asyncio.run(video._looks_like_sms_dialog(page, _sms_input(""))))

    def test_plain_tel_input_does_not_block_publishing(self):
        """页面上跟验证码无关的输入框：不弹窗，别让发布线程干等。"""
        video = _video()
        page = _page_with_get_code(0)
        self.assertFalse(asyncio.run(video._looks_like_sms_dialog(page, _sms_input("请输入联系方式"))))
        self.assertFalse(asyncio.run(video._looks_like_sms_dialog(page, _sms_input(None))))
        self.assertFalse(asyncio.run(video._looks_like_sms_dialog(page, _sms_input(""))))


if __name__ == "__main__":
    unittest.main()
