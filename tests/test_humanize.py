"""utils.humanize 的单元测试。

这层是"降低机械特征"的实现：逐字输入节奏、长文本总时长预算、定时发布时间抖动。
测试重点是**边界与健壮性**——配置写错时不能把发布流程带崩，也不该悄悄改变行为。
"""

import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from utils import humanize


class Recorder:
    """记录 keyboard 调用序列的最小替身。"""

    def __init__(self):
        self.actions = []

    async def type(self, text, delay=None):
        self.actions.append(("type", text, delay))

    async def press(self, key):
        self.actions.append(("press", key))


class FakePage:
    def __init__(self):
        self.keyboard = Recorder()


def _run(coro):
    return asyncio.run(coro)


class NormalizeDelayTest(unittest.TestCase):
    def test_none_means_disabled(self):
        self.assertIsNone(humanize.normalize_delay(None))

    def test_valid_range_passes_through(self):
        self.assertEqual(humanize.normalize_delay((45, 140)), (45, 140))
        self.assertEqual(humanize.normalize_delay([10, 20]), (10, 20))

    def test_malformed_config_falls_back_to_disabled(self):
        """配置写错（长度不对 / 类型不对 / 区间反了）不能抛异常，一律当关闭处理。"""
        for bad in ("45-140", (1,), (1, 2, 3), ("a", "b"), (100, 10), (-1, 5), {}):
            with self.subTest(bad=bad):
                self.assertIsNone(humanize.normalize_delay(bad))


class HumanTypeTest(unittest.TestCase):
    def test_disabled_uses_native_type(self):
        """delay=None → 退回原生的整体输入，保持行为可回滚。"""
        page = FakePage()
        _run(humanize.human_type(page, "整段文字", delay=None))
        self.assertEqual(page.keyboard.actions, [("type", "整段文字", None)])

    def test_types_character_by_character(self):
        page = FakePage()
        _run(humanize.human_type(page, "你好", delay=(0, 0)))
        self.assertEqual(page.keyboard.actions, [("type", "你", None), ("type", "好", None)])

    def test_newline_becomes_enter(self):
        """换行要和大平台输入语义一致：\\n 走 Enter 键，而不是打进一个字面换行符。"""
        page = FakePage()
        _run(humanize.human_type(page, "a\nb", delay=(0, 0)))
        kinds = [(k, v) for k, v, *_ in page.keyboard.actions]
        self.assertIn(("press", "Enter"), kinds)
        self.assertNotIn(("type", "\n", None), page.keyboard.actions)

    def test_empty_text_is_noop(self):
        page = FakePage()
        _run(humanize.human_type(page, "", delay=(0, 0)))
        self.assertEqual(page.keyboard.actions, [])

    def test_budget_shrinks_per_char_delay_for_long_text(self):
        """长正文要按总时长预算压缩每字延时，否则 1000 字要打两分钟、发布会被拖住。"""
        lo, hi = humanize._scale_for_budget(1000, 45, 140, budget_s=30.0)
        avg_after = (lo + hi) / 2
        self.assertLess(avg_after, 45, "超预算时必须压缩到下限以下")
        self.assertGreaterEqual(lo, 1, "压缩后每字延时不能变成 0（那就退回瞬发了）")

    def test_budget_keeps_delay_when_within_budget(self):
        lo, hi = humanize._scale_for_budget(10, 45, 140, budget_s=300.0)
        self.assertEqual((lo, hi), (45, 140))

    def test_budget_zero_is_not_a_division_by_zero(self):
        self.assertEqual(humanize._scale_for_budget(100, 45, 140, budget_s=0), (45, 140))


class JitterScheduleTest(unittest.TestCase):
    def test_disabled_returns_same_time(self):
        when = datetime(2026, 3, 10, 20, 0)
        self.assertEqual(humanize.jitter_schedule(when, None), (when, 0))
        self.assertEqual(humanize.jitter_schedule(when, 0), (when, 0))
        self.assertEqual(humanize.jitter_schedule(when, -5), (when, 0))

    def test_shift_is_forward_and_bounded(self):
        """只往后推：推到过去会被平台拒绝（定时要求至少 1 小时后）。"""
        when = datetime(2026, 3, 10, 20, 0)
        for _ in range(50):
            shifted, shift = humanize.jitter_schedule(when, 20)
            self.assertGreaterEqual(shift, 0)
            self.assertLessEqual(shift, 20)
            self.assertEqual(shifted, when + timedelta(minutes=shift))
            self.assertGreaterEqual(shifted, when)

    def test_seconds_are_floored(self):
        """秒必须归零——20:00:37 这种秒级精度本身就是非人类特征。"""
        shifted, _ = humanize.jitter_schedule(datetime(2026, 3, 10, 20, 0, 37), 0)
        self.assertEqual(shifted.second, 0)
        self.assertEqual(shifted.microsecond, 0)

    def test_jitter_actually_varies(self):
        """抖动要真的随机，不能是常数——否则等于没抖。"""
        when = datetime(2026, 3, 10, 20, 0)
        seen = {humanize.jitter_schedule(when, 20)[1] for _ in range(80)}
        self.assertGreater(len(seen), 1, "抖动结果应当有变化")

    def test_invalid_jitter_values_are_tolerated(self):
        """配置写成 0 / None 时不该抖动，也不该抛异常。"""
        when = datetime(2026, 3, 10, 20, 0)
        self.assertEqual(humanize.jitter_schedule(when, None)[1], 0)
        self.assertEqual(humanize.jitter_schedule(when, 0)[1], 0)


if __name__ == "__main__":
    unittest.main()
