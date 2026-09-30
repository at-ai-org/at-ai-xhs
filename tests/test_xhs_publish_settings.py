"""小红书发布行为设置的读取方式（uploader.xiaohongshu_uploader.settings）。

重点在**升级路径**：`conf.py` 是本地文件（已 gitignore），每个部署点都留着自己那份副本。
新加设置项时如果用裸 `from conf import XHS_XXX`，老副本会立刻 ImportError，把整条发布
链路打断——一个纯配置项的缺失不该有这个后果。所以读取必须是"有就用、没有就退回默认"，
而且默认值要是"安全的那一档"（有头、有界重试、开抖动）。
"""

import unittest
from unittest.mock import patch

from uploader.xiaohongshu_uploader import settings as xhs_settings


class _BareConf:
    """模拟"老副本 conf.py"：完全没有这些新设置项。"""


class _PartialConf:
    """模拟"只覆盖了一部分"的 conf.py。"""

    XHS_PUBLISH_MAX_ATTEMPTS = 7
    XHS_SCHEDULE_JITTER_MINUTES = 0


class _NoneConf:
    """模拟"键在但值是 None"（比如 conf 里写成了 `X = None` 占位）。"""

    XHS_PUBLISH_MAX_ATTEMPTS = None


class SettingsFallbackTest(unittest.TestCase):
    def test_missing_keys_fall_back_to_safe_defaults(self):
        with patch.object(xhs_settings, "_conf", _BareConf()):
            for name, expected in xhs_settings.DEFAULTS.items():
                with self.subTest(name=name):
                    self.assertEqual(xhs_settings._resolve(name), expected)

    def test_safe_defaults_are_the_conservative_ones(self):
        """默认值必须是收敛的那一档，缺配置时行为不能退回旧的高风险默认。"""
        self.assertGreaterEqual(
            xhs_settings.DEFAULTS["XHS_PUBLISH_MAX_ATTEMPTS"], 1, "重试必须有界"
        )
        self.assertNotEqual(
            xhs_settings.DEFAULTS["XHS_HUMAN_TYPE_DELAY"], None, "默认应开启人性化输入"
        )
        self.assertGreater(
            xhs_settings.DEFAULTS["XHS_SCHEDULE_JITTER_MINUTES"], 0, "默认应开启定时抖动"
        )

    def test_headless_is_not_a_configurable_knob(self):
        """发布一律有头是硬编码的，不该出现在可覆盖的设置表里。

        这是刻意的：留了开关迟早会被打开，然后回到被限流的老路。强制点在
        main._launch_xhs(purpose="publish") 与后端确认接口（见各自测试）。
        """
        self.assertNotIn("XHS_PUBLISH_HEADLESS", xhs_settings.DEFAULTS)
        self.assertNotIn("XHS_PUBLISH_HEADLESS", xhs_settings.__all__)

    def test_conf_value_wins_when_present(self):
        """conf 里显式配了值就用 conf 的——不能被默认值盖掉。"""
        with patch.object(xhs_settings, "_conf", _PartialConf()):
            self.assertEqual(xhs_settings._resolve("XHS_PUBLISH_MAX_ATTEMPTS"), 7)
            self.assertEqual(xhs_settings._resolve("XHS_SCHEDULE_JITTER_MINUTES"), 0)

    def test_explicit_none_falls_back_to_default(self):
        """键在但写成 None（常见占位写法）→ 退回默认，而不是把 None 传下去。"""
        with patch.object(xhs_settings, "_conf", _NoneConf()):
            self.assertEqual(
                xhs_settings._resolve("XHS_PUBLISH_MAX_ATTEMPTS"),
                xhs_settings.DEFAULTS["XHS_PUBLISH_MAX_ATTEMPTS"],
            )

    def test_zero_is_respected_not_treated_as_missing(self):
        """0 是合法取值（关抖动 / 关人性化输入），不能被当成"没配"而退回默认。"""
        with patch.object(xhs_settings, "_conf", _PartialConf()):
            self.assertEqual(xhs_settings._resolve("XHS_SCHEDULE_JITTER_MINUTES"), 0)

    def test_module_constants_are_resolved(self):
        """模块级常量要能被直接 import（main.py / page_ops.py 都这么用）。"""
        self.assertIsInstance(xhs_settings.XHS_PUBLISH_MAX_ATTEMPTS, int)
        self.assertGreaterEqual(xhs_settings.XHS_PUBLISH_MAX_ATTEMPTS, 1)
        self.assertIsInstance(xhs_settings.XHS_HUMAN_TYPE_BUDGET, float)
        self.assertIsInstance(xhs_settings.XHS_SCHEDULE_JITTER_MINUTES, int)


if __name__ == "__main__":
    unittest.main()
