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


class _FakePlaywright:
    """把 `async_playwright()` 换掉：给一个只会返回我们那张假页面的浏览器。"""

    def __init__(self, page):
        self._page = page

    def __call__(self):
        page = self._page

        class _Chromium:
            async def launch(self, **_kwargs):
                browser = MagicMock()
                browser.close = AsyncMock()
                context = MagicMock()
                context.new_page = AsyncMock(return_value=page)
                browser.new_context = AsyncMock(return_value=context)
                return browser

        class _CM:
            async def __aenter__(self):
                pw = MagicMock()
                pw.chromium = _Chromium()
                return pw

            async def __aexit__(self, *_a):
                return False

        return _CM()


def _auth_page(url="https://cp.kuaishou.com/article/publish/video", *, login_button=False, entry_ok=True):
    page = MagicMock()
    page.url = url
    page.wait_for_timeout = AsyncMock()
    page.get_by_text = MagicMock(
        return_value=MagicMock(count=AsyncMock(return_value=1 if login_button else 0))
    )
    button = MagicMock()
    button.wait_for = (
        AsyncMock() if entry_ok else AsyncMock(side_effect=RuntimeError("Timeout 25000ms exceeded"))
    )
    page.locator = MagicMock(return_value=button)
    return page


class KuaishouCookieAuthTests(unittest.TestCase):
    """「打不开 / 页面慢」**不等于**「登录态失效」。

    以前这两种情况都让 `cookie_auth()` 返回 False，调用方于是报
    「cookie文件已失效，请先完成快手登录」——网络一抖就把人骗去重新扫码，
    而账号其实是好的（真机上：一轮 4 个平台同时发，快手连着报了好几条这种）。

    分工要清楚：

    * **有证据**说未登录（跳 passport、页面出现「立即登录」、机构服务页）→ False；
    * **只是打不开/没渲染出来**（页面上没有任何未登录迹象）→ 抛 `KsPageUnreachable`，
      调用方报"打不开、账号和登录态都没问题"，用户重跑一次就好。
    """

    def _run(self, page, *, goto_error=None, cookie_invalid=False):
        for p in (
            patch.object(ks_main, "async_playwright", _FakePlaywright(page)),
            patch.object(ks_main, "set_init_script", AsyncMock(side_effect=lambda ctx: ctx)),
            patch.object(ks_main, "_is_ks_cookie_invalid", AsyncMock(return_value=cookie_invalid)),
            patch.object(ks_main, "_goto_ks_page", AsyncMock(side_effect=goto_error)),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_navigation_failure_is_not_a_cookie_problem(self):
        page = _auth_page()
        self._run(page, goto_error=RuntimeError("Page.goto: net::ERR_CONNECTION_CLOSED"))

        with self.assertRaises(ks_main.KsPageUnreachable):
            asyncio.run(ks_main.cookie_auth("fake.json"))

    def test_slow_page_without_login_evidence_is_not_a_cookie_problem(self):
        """页面打开了、也没说未登录，只是上传入口一直没渲染出来——多半是机器忙。"""
        page = _auth_page(entry_ok=False)
        self._run(page)

        with self.assertRaises(ks_main.KsPageUnreachable):
            asyncio.run(ks_main.cookie_auth("fake.json"))

    def test_redirect_to_passport_means_really_expired(self):
        page = _auth_page(url="https://passport.kuaishou.com/pc/account/login/?sid=x")
        self._run(page)
        self.assertIs(asyncio.run(ks_main.cookie_auth("fake.json")), False)

    def test_login_button_means_really_expired(self):
        page = _auth_page(login_button=True)
        self._run(page)
        self.assertIs(asyncio.run(ks_main.cookie_auth("fake.json")), False)

    def test_upload_entry_means_valid(self):
        page = _auth_page()
        self._run(page)
        self.assertIs(asyncio.run(ks_main.cookie_auth("fake.json")), True)


class KuaishouUploadEntryTests(unittest.TestCase):
    def test_button_appearing_late_is_not_a_failure(self):
        """按钮慢一两拍才出现（并发跑多个浏览器时的常态）→ 等它，不要判死。"""
        locator = _Locator(invisible_times=1)
        with patch.object(ks_main.asyncio, "sleep", AsyncMock()), patch.object(
            ks_main, "_dump_page_debug", AsyncMock(return_value="screenshots/kuaishou/fake")
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
            ks_main, "_dump_page_debug", AsyncMock(return_value="screenshots/kuaishou/fake")
        ) as dump:
            with self.assertRaisesRegex(RuntimeError, "screenshots/kuaishou/fake"):
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
            ks_main, "_dump_page_debug", AsyncMock(return_value="screenshots/kuaishou/fake")
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
