# Language: 中文
"""发布失败取证的契约测试：**五个平台的现场截图必须都落在 `screenshots/<平台>/`**。

为什么值得单独钉住
------------------
这条链路本身不难，难的是"每次加平台/改路径时没人记得它"。改之前的样子是：拼多多的现场
在 `pdd_debug/`、快手选择器对不上的现场在 `ks_debug/`、视频号三张图散在**仓库根目录**、
抖音和快手的失败截图平铺在 `screenshots/` 根下（一百多张混在一起，光看文件名分不出是谁的）。
排查一条失败任务要先把这四个地方都翻一遍。

所以这里的测试盯三件事：

1. **目录只有一处定义**：`myUtils/publish_evidence.PLATFORM_DIRS` 的键必须和
   `sau_backend_publish.PLATFORMS[*]["business"]`（= `logs/<平台>.log`、
   `cookiesFile/<平台>_*.json` 用的名字）完全一致，不许各写各的；
2. **取证绝不许把发布搞失败**：截图超时/异常只记日志、返回空串——2026-09-29 抖音那条
   真事故就是取证截图自己抛异常、把一次本可以重试成功的发布判死的
   （详见 `tests/test_first_round_timeouts.py`）；
3. **各个平台的上传器都接上了**：五个平台的发布路径上都必须有取证调用，且不许再出现
   `pdd_debug` / `ks_debug` / 根目录 `debug_tencent_*.png` 这类"平台自己另起一个外层目录"。
"""

import asyncio
import inspect
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import myUtils.publish_evidence as evidence


async def _write_shot(**kwargs):
    """假 Playwright 的 `page.screenshot(path=…)`：真的落一个文件。

    必须真的写盘——"同一秒不互相覆盖"那条契约靠的就是"目标文件已存在"，
    只记一个 AsyncMock 调用的话，第二次截图看起来和第一次一模一样。
    """
    Path(kwargs["path"]).write_bytes(b"png")


class PlatformDirContractTests(unittest.TestCase):
    def test_every_publish_center_platform_has_a_evidence_dir(self):
        """发布中心认得的每个平台，都得能在 `screenshots/` 下有自己的一格。"""
        from sau_backend_publish import PLATFORMS

        businesses = {item["business"] for item in PLATFORMS.values()}
        self.assertTrue(
            businesses <= set(evidence.PLATFORM_DIRS),
            f"这些平台还没有取证目录: {sorted(businesses - set(evidence.PLATFORM_DIRS))}",
        )

    def test_platform_dirs_are_flat_ascii_names(self):
        """目录名必须是一层、纯 ASCII：`screenshots/<平台>/`，不再有外层目录。"""
        for platform, directory in evidence.PLATFORM_DIRS.items():
            self.assertEqual(directory, platform)
            self.assertNotIn("/", directory)
            self.assertTrue(directory.isascii(), directory)

    def test_unknown_platform_is_rejected_loudly(self):
        """写错平台名要当场报错，不能悄悄落一个 `screenshots/unknown/`。"""
        with self.assertRaises(ValueError):
            evidence.platform_dir("douyinn")

    def test_dir_hint_is_the_relative_path_users_see(self):
        self.assertEqual(evidence.dir_hint("douyin"), "screenshots/douyin/")
        self.assertEqual(
            evidence.describe("pdd", "upload-stalled"),
            "现场已存到 screenshots/pdd/（upload-stalled）",
        )

    def test_tags_are_filename_safe(self):
        self.assertEqual(evidence.safe_tag("abort/fill:desc"), "abort_fill_desc")
        self.assertEqual(evidence.safe_tag(""), "shot")


class CaptureContractTests(unittest.TestCase):
    """`capture()` 的两条硬契约：落对地方、且**永不外溢异常**。"""

    def _page(self, screenshot=None):
        page = AsyncMock()
        page.url = "https://example.com/publish"
        page.screenshot = screenshot or AsyncMock(side_effect=_write_shot)
        return page

    def test_screenshot_lands_in_the_platform_dir(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "screenshots"
            page = self._page()
            with patch.object(evidence, "evidence_root", lambda: root):
                path = asyncio.run(evidence.capture(page, "douyin", "publish-failed"))

            self.assertTrue(path.startswith(str(root / "douyin")))
            self.assertTrue(path.endswith("-publish-failed.png"))
            # 全页 + 自带超时：截图前 Playwright 要等字体，没有自己的超时就会撞默认 30 秒
            kwargs = page.screenshot.await_args.kwargs
            self.assertTrue(kwargs["full_page"])
            self.assertEqual(kwargs["timeout"], evidence.DEFAULT_SHOT_TIMEOUT_MS)

    def test_html_is_written_next_to_the_screenshot_when_asked(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "screenshots"
            page = self._page()
            page.content = AsyncMock(return_value="<html>现场</html>")
            with patch.object(evidence, "evidence_root", lambda: root):
                path = asyncio.run(evidence.capture(page, "pdd", "failed", html=True))

            html = Path(path).with_suffix(".html")
            self.assertEqual(html.read_text(encoding="utf-8"), "<html>现场</html>")

    def test_screenshot_timeout_never_propagates(self):
        """现场 id=84：截图等字体超时 → 以前异常从 `except` 冒出去，把发布判死。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            page = self._page(
                AsyncMock(side_effect=TimeoutError("waiting for fonts to load"))
            )
            with patch.object(evidence, "evidence_root", lambda: Path(tmp) / "screenshots"):
                self.assertEqual(
                    asyncio.run(evidence.capture(page, "kuaishou", "unit-test")), ""
                )

    def test_missing_page_is_not_an_error(self):
        """还没开浏览器就失败（比如 cookie 文件不在）时没有页面可拍，不该再抛一次。"""
        self.assertEqual(asyncio.run(evidence.capture(None, "pdd", "no-page")), "")

    def test_same_second_captures_do_not_overwrite_each_other(self):
        """同一秒里两条任务同时失败（发布中心是并行跑的）不能互相覆盖。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "screenshots"
            page = self._page()
            with patch.object(evidence, "evidence_root", lambda: root):
                first = asyncio.run(evidence.capture(page, "tencent", "failed"))
                second = asyncio.run(evidence.capture(page, "tencent", "failed"))

            self.assertNotEqual(first, second)
            self.assertEqual(len(list((root / "tencent").glob("*.png"))), 2)

    def test_failed_screenshot_leaves_no_empty_placeholder(self):
        """截图失败时不能留下 0 字节的占位 .png——那比"什么都没有"更误导排查的人。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "screenshots"
            page = self._page(
                AsyncMock(side_effect=TimeoutError("waiting for fonts to load"))
            )
            with patch.object(evidence, "evidence_root", lambda: root):
                asyncio.run(evidence.capture(page, "pdd", "failed"))

            self.assertEqual(list((root / "pdd").glob("*.png")), [])


class EveryPlatformLeavesEvidenceTests(unittest.TestCase):
    """五个平台的发布路径上都得有取证调用——少一个就是"那个平台出事时什么都没有"。"""

    def test_uploaders_call_the_shared_capture(self):
        from uploader.douyin_uploader import main as douyin_main
        from uploader.ks_uploader import main as ks_main
        from uploader.pdd_uploader import main as pdd_main
        from uploader.tencent_uploader import main as tencent_main
        from uploader.xiaohongshu_uploader import main as xhs_main
        from uploader.xiaohongshu_uploader import page_ops as xhs_page_ops

        modules = {
            "xiaohongshu": (xhs_main, xhs_page_ops),
            "tencent": (tencent_main,),
            "douyin": (douyin_main,),
            "kuaishou": (ks_main,),
            "pdd": (pdd_main,),
        }
        for platform, items in modules.items():
            source = "\n".join(inspect.getsource(module) for module in items)
            self.assertIn(
                "publish_evidence",
                source,
                f"{platform} 的上传器没有接上统一取证（myUtils/publish_evidence）",
            )

    def test_no_platform_specific_outer_directory_is_left(self):
        """`pdd_debug/`、`ks_debug/`、根目录的 `debug_tencent_*.png` 都不许再出现。"""
        from uploader.douyin_uploader import main as douyin_main
        from uploader.ks_uploader import main as ks_main
        from uploader.pdd_uploader import main as pdd_main
        from uploader.tencent_uploader import main as tencent_main

        for module in (douyin_main, ks_main, pdd_main, tencent_main):
            source = inspect.getsource(module)
            for forbidden in ('"pdd_debug"', "'pdd_debug'", '"ks_debug"', "'ks_debug'"):
                self.assertNotIn(forbidden, source, f"{module.__name__} 还在用旧的外层目录")
            self.assertNotIn(
                'BASE_DIR) / "debug_tencent',
                source,
                f"{module.__name__} 还在往仓库根目录丢截图",
            )

    def test_failure_message_tells_the_user_where_to_look(self):
        """发布中心的失败结论里要带现场目录——用户不用回头翻日志猜。"""
        import sau_backend_publish as backend

        self.assertEqual(backend._evidence_hint_of(3), "（现场截图见 screenshots/douyin/）")
        self.assertEqual(backend._evidence_hint_of(5), "（现场截图见 screenshots/pdd/）")
        # 查不到就不印：结论里出现一个不存在的目录比不印更误导
        self.assertEqual(backend._evidence_hint_of(99), "")


if __name__ == "__main__":
    unittest.main()
