# Language: 中文
"""「一批任务里第一条必挂」的回归测试（2026-09-29 现场）。

## 现场长什么样

Web 端一次提交 3 个平台 × 2 条素材 = 6 条任务。每个通道（一个平台的一个账号）内部串行，
通道之间并行 —— 所以**每个通道的第一条**在同一秒同时启动，各自拉一个 chromium。
机器最冷的时刻刚好落在这一轮：新进程、磁盘缓存冷、DNS/TLS 冷、CPU 互相抢。

DB 里的记录（job=304531fa）：

| 记录 | 平台 | 轮次 | 结果 |
|---|---|---|---|
| 83 | 视频号 | 第 1 条 | 成功（cookie 校验花了 32 秒） |
| 84 | 抖音 | 第 1 条 | **失败：`Page.screenshot: Timeout 30000ms exceeded ... waiting for fonts to load`** |
| 85 | 快手 | 第 1 条 | **失败：`Page.goto: Timeout 30000ms exceeded ... waiting until "load"`** |
| 86/87/88 | 视频号/抖音/快手 | 第 2 条 | 全部成功 |

同一条快手的 `goto`：第一轮 30 秒没等到 `load`，第二轮只用了 10 秒。

## 两个根因（都是"超时值写死 + 拿默认值"）

1. **快手**：`await page.goto(KUAISHOU_UPLOAD_URL)` 是 Playwright 的默认值
   ——`wait_until="load"` + 30 秒。`load` 要等字体/图片/统计脚本全部就绪，
   并发跑多个浏览器时这些恰好最慢。同一次提交里抖音那条写的是
   `domcontentloaded` + 90 秒，它就没死在导航上。
2. **抖音/快手**：发布重试分支里的 `await page.screenshot(full_page=True)`
   ——没有 path（字节直接丢掉），也没有自己的超时。Playwright 截图前要等字体，
   机器忙时一路撞到默认 30 秒超时抛异常；而它位于 `except` 分支里，
   异常直接冒出去把"再试一次就会成功"的发布判成失败。

这个文件钉住现在的契约：
- 首次导航必须显式给 `domcontentloaded` + 90 秒，并且失败要重来一次；
- 取证截图必须有界、必须落盘、**异常绝不允许外溢**；
- 抖音的发布重试要有总预算（不再是无限 `while True`）。
"""

import asyncio
import io
import tokenize
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from uploader.douyin_uploader import main as douyin_main
from uploader.douyin_uploader.main import DouYinVideo
from uploader.ks_uploader import main as ks_main


def _code_only(module) -> str:
    """把源码里的注释和字符串都挖掉，只留真正的代码。

    源码级护栏必须这么做：这两个文件里到处是"原来写的是 `await page.goto(...)`"
    这类说明文字，直接搜源码会把说明文字也当成违规代码。
    """
    source = Path(module.__file__).read_text(encoding="utf-8")
    lines = source.splitlines()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (start_row, start_col), (end_row, end_col) = token.start, token.end
        if start_row == end_row:
            line = lines[start_row - 1]
            lines[start_row - 1] = line[:start_col] + " " * (end_col - start_col) + line[end_col:]
        else:
            lines[start_row - 1] = lines[start_row - 1][:start_col]
            for row in range(start_row, end_row - 1):
                lines[row] = ""
            lines[end_row - 1] = " " * end_col + lines[end_row - 1][end_col:]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 快手：首次导航
# --------------------------------------------------------------------------- #


def _ks_page(side_effects=None, url: str = "https://cp.kuaishou.com/article/publish/video"):
    page = MagicMock()
    page.url = url
    page.goto = AsyncMock(side_effect=side_effects) if side_effects else AsyncMock()
    page.wait_for_url = AsyncMock()
    page.screenshot = AsyncMock()
    return page


class KuaishouFirstNavigationTests(unittest.TestCase):
    def test_navigation_uses_domcontentloaded_with_generous_timeout(self):
        """不能再拿 Playwright 的默认值（load + 30 秒）去赌并发下的首轮加载。"""
        page = _ks_page()
        asyncio.run(ks_main._goto_ks_page(page))
        self.assertEqual(page.goto.await_count, 1)
        self.assertEqual(page.goto.await_args.args[0], ks_main.KUAISHOU_UPLOAD_URL)
        self.assertEqual(page.goto.await_args.kwargs["wait_until"], "domcontentloaded")
        self.assertEqual(page.goto.await_args.kwargs["timeout"], ks_main.KUAISHOU_NAV_TIMEOUT_MS)
        self.assertGreaterEqual(ks_main.KUAISHOU_NAV_TIMEOUT_MS, 60_000)

    def test_first_navigation_timeout_recovers_on_retry(self):
        """首轮导航超时（并发下的常态）→ 自动重来一次就成功，不许判死。"""
        page = _ks_page(side_effects=[TimeoutError("Page.goto: Timeout 90000ms exceeded"), None])
        with patch.object(ks_main.asyncio, "sleep", AsyncMock()):
            asyncio.run(ks_main._goto_ks_page(page, what="快手上传主页"))
        self.assertEqual(page.goto.await_count, 2)

    def test_navigation_gives_up_only_after_retries_with_actionable_reason(self):
        """两次都打不开才失败，并且要说清"这不是 cookie 失效"。"""
        page = _ks_page(side_effects=TimeoutError("Page.goto: Timeout 90000ms exceeded"))
        with patch.object(ks_main.asyncio, "sleep", AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "SAU_MAX_CONCURRENT_BROWSERS"):
                asyncio.run(ks_main._goto_ks_page(page, what="快手上传主页"))
        self.assertEqual(page.goto.await_count, ks_main.KUAISHOU_NAV_ATTEMPTS)

    def test_login_redirect_is_reported_as_login_failure(self):
        """被踹到登录页时要直说"去重新登录"，不能让人对着 URL 超时日志猜。"""
        page = _ks_page(url="https://passport.kuaishou.com/pc/account/login/?sid=kuaishou.web.cp.api")
        page.wait_for_url = AsyncMock(side_effect=TimeoutError("waiting for url"))
        with self.assertRaisesRegex(RuntimeError, "重新登录"):
            asyncio.run(ks_main._wait_ks_upload_page(page))

    def test_no_bare_goto_left_in_kuaishou_uploader(self):
        """源码级的护栏：默认超时那个坑不许再被写回来。"""
        source = _code_only(ks_main)
        self.assertNotIn("await page.goto(KUAISHOU_UPLOAD_URL)", source)
        self.assertNotIn("await page.goto(KUAISHOU_LOGIN_URL)", source)


# --------------------------------------------------------------------------- #
# 取证截图：有界、落盘、异常不外溢
# --------------------------------------------------------------------------- #


class DebugShotTests(unittest.TestCase):
    def test_screenshot_timeout_never_propagates(self):
        page = MagicMock()
        page.screenshot = AsyncMock(side_effect=TimeoutError("waiting for fonts to load"))
        self.assertEqual(asyncio.run(ks_main._debug_shot(page, "ks-test")), "")
        self.assertEqual(asyncio.run(douyin_main._debug_shot(page, "douyin-test")), "")

    def test_screenshot_is_bounded_and_actually_saved(self):
        """原来写的是 `page.screenshot(full_page=True)`——没有 path，字节直接丢掉。"""
        for module, timeout_ms in (
            (ks_main, ks_main.KUAISHOU_DEBUG_SHOT_TIMEOUT_MS),
            (douyin_main, douyin_main.DOUYIN_DEBUG_SHOT_TIMEOUT_MS),
        ):
            page = MagicMock()
            page.screenshot = AsyncMock(return_value=b"png")
            asyncio.run(module._debug_shot(page, "unit-test"))
            kwargs = page.screenshot.await_args.kwargs
            self.assertIn("path", kwargs)
            self.assertEqual(kwargs["timeout"], timeout_ms)
            self.assertLessEqual(timeout_ms, 10_000)

    def test_no_unbounded_full_page_screenshot_left(self):
        for module in (ks_main, douyin_main):
            self.assertNotIn("await page.screenshot(full_page=True)", _code_only(module))


# --------------------------------------------------------------------------- #
# 抖音：发布重试
# --------------------------------------------------------------------------- #


def _douyin_page(wait_for_url_effects, screenshot_side_effect=None):
    page = MagicMock()
    page.evaluate = AsyncMock()
    # 短信弹窗那条路：没有验证码输入框 → 不触发
    sms_input = MagicMock()
    sms_input.count = AsyncMock(return_value=0)
    sms_input.is_visible = AsyncMock(return_value=False)
    locator = MagicMock()
    locator.first = sms_input
    page.locator.return_value = locator
    page.get_by_text.return_value = locator  # handle_auto_video_cover 的探测：看不见
    locator.is_visible = AsyncMock(return_value=False)

    publish_button = MagicMock()
    publish_button.count = AsyncMock(return_value=1)
    publish_button.click = AsyncMock()
    page.get_by_role.return_value = publish_button

    page.wait_for_url = AsyncMock(side_effect=wait_for_url_effects)
    page.screenshot = AsyncMock(side_effect=screenshot_side_effect)
    return page, publish_button


class DouyinPublishRetryTests(unittest.TestCase):
    def _video(self) -> DouYinVideo:
        video = DouYinVideo("标题", "/tmp/demo.mp4", [], 0, "/tmp/cookie.json")
        video.debug = True  # 现场就是默认开着 debug（DEBUG_MODE=True）
        return video

    def test_screenshot_timeout_does_not_kill_the_publish_retry(self):
        """现场 id=84：截图等字体超时 → 异常从 except 冒出去 → 整条发布判死。"""
        page, publish_button = _douyin_page(
            wait_for_url_effects=[TimeoutError("Timeout 10000ms exceeded"), None],
            screenshot_side_effect=TimeoutError("waiting for fonts to load"),
        )
        with patch.object(douyin_main.asyncio, "sleep", AsyncMock()):
            asyncio.run(self._video().publish_with_sms_guard(page))

        self.assertEqual(page.wait_for_url.await_count, 2, "第一次没确认成功就该再试一次")
        self.assertGreaterEqual(publish_button.click.await_count, 2)

    def test_cover_recovery_failure_does_not_kill_the_retry_either(self):
        """retry 分支里的补救动作（处理推荐封面）同样不许把循环带走。"""
        page, _ = _douyin_page(wait_for_url_effects=[TimeoutError("boom"), None])
        video = self._video()
        video.handle_auto_video_cover = AsyncMock(side_effect=RuntimeError("封面弹窗炸了"))
        with patch.object(douyin_main.asyncio, "sleep", AsyncMock()):
            asyncio.run(video.publish_with_sms_guard(page))
        self.assertEqual(page.wait_for_url.await_count, 2)

    def test_retry_is_bounded(self):
        """不再是 `while True`：平台真拦住时要带着原因失败，而不是点到看门狗来收尸。"""
        page, _ = _douyin_page(wait_for_url_effects=TimeoutError("被平台拦住了"))
        with patch.object(douyin_main.asyncio, "sleep", AsyncMock()), patch.object(
            douyin_main, "DOUYIN_PUBLISH_TIMEOUT_SECONDS", 0
        ):
            with self.assertRaisesRegex(RuntimeError, "停止重试"):
                asyncio.run(self._video().publish_with_sms_guard(page))
        self.assertEqual(page.wait_for_url.await_count, 0)


if __name__ == "__main__":
    unittest.main()
