# Language: 中文
"""抖音「自主声明」默认值的回归测试。

用户的要求：实拍视频不声明 AI 生成内容，把默认的「内容由AI生成」关掉。
以前 `upload()` 里有一句无条件兜底：

    if not self.declaration:
        self.declaration = "内容由AI生成"

等于给每一条实拍视频都挂了块 AI 牌子。现在默认什么都不声明；
只有明确配了文案（模型设置 → 发布默认值 → 抖音自主声明，或构造上传器时传
`declaration=…`）才去动页面上那个单选。
"""

import asyncio
import ast
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from uploader.douyin_uploader import main as douyin_main
from uploader.douyin_uploader.main import DouYinVideo
from myUtils.app_settings import DEFAULT_SETTINGS, get_douyin_declaration


def _forced_declaration_assignments() -> list[int]:
    """源码里"自己给自己塞一条 AI 声明"的赋值语句行号（应当为空）。

    用 AST 而不是字符串查找：注释里正当地提到了那句老代码（解释为什么删掉它），
    字符串查找会把注释也算成"还在"。
    """
    tree = ast.parse(Path(douyin_main.__file__).read_text(encoding="utf-8"))
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "declaration"
                and isinstance(node.value, ast.Constant)
                and "AI" in str(node.value.value)
            ):
                lines.append(node.lineno)
    return lines


class DouyinDeclarationDefaultTests(unittest.TestCase):
    def test_upload_no_longer_forces_an_ai_declaration(self):
        """那句兜底必须真的没了——它是"实拍视频被挂 AI 牌子"的唯一来源。"""
        self.assertEqual(_forced_declaration_assignments(), [])

    def test_setting_defaults_to_no_declaration(self):
        self.assertEqual(DEFAULT_SETTINGS.get("douyin_declaration"), "")
        self.assertEqual(get_douyin_declaration(), "")

    def test_default_does_not_touch_the_page(self):
        video = DouYinVideo("标题", "/tmp/demo.mp4", [], 0, "/tmp/cookie.json")
        self.assertIsNone(video.declaration)
        video.set_self_declaration = AsyncMock()
        asyncio.run(video.apply_self_declaration(object()))
        video.set_self_declaration.assert_not_awaited()

    def test_explicit_declaration_is_still_honoured(self):
        video = DouYinVideo(
            "标题", "/tmp/demo.mp4", [], 0, "/tmp/cookie.json", declaration="内容由AI生成"
        )
        page = object()
        video.set_self_declaration = AsyncMock(return_value=True)
        asyncio.run(video.apply_self_declaration(page))
        video.set_self_declaration.assert_awaited_once_with(page, "内容由AI生成")

    def test_explicit_declaration_failure_still_blocks_publish(self):
        """显式要求声明却没设上：宁可不发，也不能发出一条没声明的视频。"""
        video = DouYinVideo(
            "标题", "/tmp/demo.mp4", [], 0, "/tmp/cookie.json", declaration="内容由AI生成"
        )
        video.set_self_declaration = AsyncMock(return_value=False)
        with self.assertRaisesRegex(RuntimeError, "自主声明"):
            asyncio.run(video.apply_self_declaration(object()))


if __name__ == "__main__":
    unittest.main()
