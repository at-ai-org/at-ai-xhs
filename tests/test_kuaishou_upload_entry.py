# Language: 中文
"""快手发布页「上传入口按钮」等待逻辑的回归测试。

这个选择器曾经是 `wait_for(state="visible", timeout=10000)`，2026-09-26 09:05 直接把
一条发布判死：`Locator.wait_for: Timeout 10000ms exceeded ... button[class^='_upload-btn']`。
可是 16 秒前同一个账号刚在 cookie 校验里见过这个按钮——选择器没坏，是**页面还没渲染完**，
而当时视频号/抖音/快手三个浏览器正在并行跑。

所以现在的契约是：
- 等不到就多等、必要时重新加载页面再来一轮，最后才报错；
- 跳到登录页要立刻说清是"登录失效"，不能让人对着超时日志瞎猜；
- 报错前一定留下现场截图/HTML 路径。
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from uploader.ks_uploader import main as ks_main


class _Locator:
    """模拟 Locator：前 n 次查询说"看不见"，之后说"看得见"。"""

    def __init__(self, invisible_times: int = 0):
        self.remaining = invisible_times
        self.first = self

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        if self.remaining > 0:
            self.remaining -= 1
            return False
        return True


def _page(url: str = "https://cp.kuaishou.com/article/publish/video"):
    page = MagicMock()
    page.url = url
    page.reload = AsyncMock()
    page.goto = AsyncMock()
    return page


class KuaishouUploadEntryTests(unittest.TestCase):
    def test_button_appearing_late_is_not_a_failure(self):
        """按钮慢一两拍才出现（并发跑多个浏览器时的常态）→ 等它，不要判死。"""
        locator = _Locator(invisible_times=1)
        with patch.object(ks_main.asyncio, "sleep", AsyncMock()), patch.object(
            ks_main, "_dump_page_debug", AsyncMock(return_value="ks_debug/fake")
        ) as dump:
            asyncio.run(
                ks_main._wait_upload_entry(_page(), locator, what="「上传视频」按钮", timeout_ms=2000)
            )
        dump.assert_not_called()

    def test_login_page_fails_fast_with_actionable_message(self):
        locator = _Locator(invisible_times=10**6)
        page = _page(url="https://passport.kuaishou.com/pc/account/login/?sid=kuaishou.web.cp.api")
        with patch.object(ks_main.asyncio, "sleep", AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "重新登录"):
                asyncio.run(
                    ks_main._wait_upload_entry(page, locator, what="「上传视频」按钮", timeout_ms=500)
                )

    def test_gives_up_only_after_retry_and_keeps_evidence(self):
        locator = _Locator(invisible_times=10**6)
        on_retry = AsyncMock()
        with patch.object(ks_main.asyncio, "sleep", AsyncMock()), patch.object(
            ks_main, "_dump_page_debug", AsyncMock(return_value="ks_debug/fake")
        ) as dump:
            with self.assertRaisesRegex(RuntimeError, "ks_debug/fake"):
                asyncio.run(
                    ks_main._wait_upload_entry(
                        _page(),
                        locator,
                        what="「上传视频」按钮",
                        attempts=2,
                        timeout_ms=300,
                        on_retry=on_retry,
                    )
                )
        # 两轮各存一次现场；中间那轮按调用方给的动作"恢复到入口"
        self.assertEqual(dump.await_count, 2)
        on_retry.assert_awaited_once()

    def test_note_tab_retry_does_not_reload_the_page(self):
        """图文那条路不能 reload：一刷新就回到「视频」标签，上传图片按钮永远不出现。"""
        locator = _Locator(invisible_times=10**6)
        page = _page()
        with patch.object(ks_main.asyncio, "sleep", AsyncMock()), patch.object(
            ks_main, "_dump_page_debug", AsyncMock(return_value="ks_debug/fake")
        ), patch.object(ks_main, "_switch_to_note_tab", AsyncMock()) as switch:
            with self.assertRaises(RuntimeError):
                asyncio.run(
                    ks_main._wait_upload_entry(
                        page,
                        locator,
                        what="「上传图片」按钮",
                        attempts=2,
                        timeout_ms=300,
                        on_retry=lambda: ks_main._switch_to_note_tab(page),
                    )
                )
        switch.assert_awaited_once()
        page.reload.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
