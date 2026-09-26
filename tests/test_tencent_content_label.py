# Language: 中文
"""视频号「视频标注」默认值的回归测试。

背景：这些素材是**实拍**视频，没有 AI 生成内容，视频号也不强制标注。
以前上传器无脑去点「含AI生成内容」，选项根本不存在时白等 5 秒再抛一条
`Locator.wait_for: Timeout 5000ms exceeded` 的警告刷在日志里，看着像坏了。
现在默认什么都不点；只有明确配了标注文案（模型设置 → 发布默认值）才去动页面。
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from uploader.tencent_uploader.main import TencentBaseUploader, TencentVideo


def _fake_page(entry_mock, option_mock):
    page = MagicMock()
    page.wait_for_timeout = AsyncMock()

    def get_by_text(text, **kwargs):
        return entry_mock if text == "选择视频标注" else option_mock

    page.get_by_text.side_effect = get_by_text
    return page


class TencentContentLabelTests(unittest.TestCase):
    def test_default_does_not_touch_the_page_at_all(self):
        uploader = TencentBaseUploader(publish_date=0, account_file="unused")
        self.assertEqual(uploader.content_label, "")

        page = _fake_page(MagicMock(), MagicMock())
        asyncio.run(uploader.apply_original_statement(page))

        # 入口都没查过：不该为了一个不存在的选项在页面上瞎点、白等超时
        page.get_by_text.assert_not_called()

    def test_explicit_label_still_works_when_asked_for(self):
        uploader = TencentVideo(
            "标题", "/tmp/demo.mp4", [], 0, "/tmp/c.json", content_label="含AI生成内容"
        )
        self.assertEqual(uploader.content_label, "含AI生成内容")

        entry = MagicMock()
        entry.first = entry
        entry.count = AsyncMock(return_value=1)
        entry.click = AsyncMock()
        option = MagicMock()
        option.first = option
        option.wait_for = AsyncMock()
        option.click = AsyncMock()

        asyncio.run(uploader.apply_original_statement(_fake_page(entry, option)))

        entry.click.assert_awaited_once()
        option.click.assert_awaited_once()

    def test_missing_entry_on_page_is_skipped_not_fatal(self):
        uploader = TencentVideo(
            "标题", "/tmp/demo.mp4", [], 0, "/tmp/c.json", content_label="含AI生成内容"
        )
        entry = MagicMock()
        entry.first = entry
        entry.count = AsyncMock(return_value=0)   # 页面没有「视频标注」入口
        option = MagicMock()

        # 不抛异常：发布流程不能被一个可选标注拦住
        asyncio.run(uploader.apply_original_statement(_fake_page(entry, option)))
        option.click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
