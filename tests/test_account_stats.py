"""账号数据（粉丝 / 互动）详情：抓取契约、缓存、Web 接口（at-ai-xhs 改造新增）。

这个功能真正会坏的地方**不在解析，而在三件容易被忽略的事**，所以测试重点也在这三处：

1. **四个平台的数字类型不一样**：快手返回浮点（`6763.0`）、抖音返回字符串（`"40"`）、
   小红书直接给整数。不统一的话界面上会冒出 "6763.0 次播放"，或者更糟——前端拿
   `"188"` 去做加法变成 `"1881"`。所以 `canonicalize` 的行为要钉住。
2. **"没有这个数据"和"这个数据是 0"必须分开**。快手没有作品级分享数、没有 30 天口径；
   视频号可能连粉丝数都拿不到。这些必须表现为**字段不存在**，而不是 0——
   显示成 0 会让人以为账号真的零分享、零粉丝。
3. **缓存决定"点一下等多久"**。详情第一次要开浏览器（几秒到十几秒），之后必须命中缓存；
   缓存过期时也不能让用户干等，要**把旧数据一起返回**（`stale=True`）。

平台适配器里**真正抓取的部分不在这里测**（那要开浏览器、还依赖平台在线，
不适合进单元测试）。测的是它们周边的纯逻辑：类型归一化、响应解析、缓存、Web 层编排。
抖音的解析用了**真实抓到的响应形状**（字段名和值是从线上响应里抄下来的），
视频号用的则是从平台自己的前端 bundle 里读出来的字段名——它当前是登出状态，
没有真实响应可用（见 `myUtils/account_stats/tencent.py` 的说明）。
"""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from unittest.mock import patch

from myUtils.account_stats import cache
from myUtils.account_stats import sync as stats_sync
from myUtils.account_stats.base import (
    AccountStatsError,
    StatsUnavailable,
    build_stats,
    canonicalize,
    load_adapter,
    platform_name,
    platform_supported,
)
from myUtils.account_stats.douyin import _item as douyin_item
from myUtils.account_stats.douyin import _overview_period as douyin_period
from myUtils.account_stats.kuaishou import _overview_period as ks_period
from myUtils.account_stats.kuaishou import _photo_item as ks_item
from myUtils.account_stats.tencent import _daily_series, _period_from_total_data, _post_item
from myUtils.account_stats.xhs import _fans_section, _media_item


# --------------------------------------------------------------------------- #
# 类型归一化
# --------------------------------------------------------------------------- #


class CanonicalizeTest(unittest.TestCase):
    def test_whole_floats_become_ints(self):
        """快手把计数返回成浮点，界面不能显示成 "6763.0"。"""
        self.assertEqual(canonicalize(6763.0), 6763)
        self.assertIsInstance(canonicalize(6763.0), int)
        self.assertEqual(canonicalize({"play": 18.0}), {"play": 18})

    def test_real_decimals_survive(self):
        """完播率 0.0432 不能被当成"像计数"而转成 0。"""
        self.assertAlmostEqual(canonicalize(0.0432), 0.0432)
        self.assertAlmostEqual(canonicalize(4.32), 4.32)
        # 浮点误差的长尾要砍掉，否则界面上会出现 0.043200000000000002
        self.assertEqual(canonicalize(0.043200000000000002), 0.0432)

    def test_bools_are_not_ints(self):
        """`True` 是 `int` 的子类，转成 1 会让 `supported: true` 变成 `1`。"""
        self.assertIs(canonicalize(True), True)
        self.assertIs(canonicalize({"supported": False})["supported"], False)

    def test_recurses_into_lists_and_dicts(self):
        result = canonicalize({"a": [1.0, 2.5, {"b": 3.0}]})
        self.assertEqual(result, {"a": [1, 2.5, {"b": 3}]})


# --------------------------------------------------------------------------- #
# 结构化契约
# --------------------------------------------------------------------------- #


class BuildStatsTest(unittest.TestCase):
    def test_shape_is_always_complete(self):
        """缺哪段都要留**空壳**，前端才不会到处判空。"""
        stats = build_stats(platform_no=1, account_name="某人")
        for key in ("account", "fans", "engagement", "media", "extra", "notes"):
            self.assertIn(key, stats)
        self.assertEqual(stats["media"]["items"], [])
        self.assertFalse(stats["media"]["supported"])
        self.assertEqual(stats["notes"], [])
        self.assertTrue(stats["fetched_at"])

    def test_platform_name_comes_from_project_numbering(self):
        """编号沿用项目里那一套：1 小红书 2 视频号 3 抖音 4 快手。"""
        self.assertEqual(platform_name(1), "小红书")
        self.assertEqual(platform_name(2), "视频号")
        self.assertEqual(platform_name(3), "抖音")
        self.assertEqual(platform_name(4), "快手")
        self.assertEqual(platform_name(99), "未知平台")

    def test_every_platform_has_an_adapter_with_fetch(self):
        for platform_no in (1, 2, 3, 4):
            self.assertTrue(platform_supported(platform_no))
            module = load_adapter(platform_no)
            self.assertTrue(callable(module.fetch), f"平台 {platform_no} 的适配器没有 fetch")

    def test_unknown_platform_is_unsupported_not_a_crash(self):
        """没适配器的平台要"提示"而不是抛异常。

        注意编号：5 原本是"没有适配器"的例子，**2026-10-09 起拼多多接上了适配器**
        （`account_stats/pdd.py`，和快手一样开浏览器收页面自己的响应），
        所以这里换成一个真正不存在的编号。
        """
        self.assertFalse(platform_supported(99))
        with self.assertRaises(StatsUnavailable):
            load_adapter(99)

    def test_pdd_is_supported_since_it_has_an_adapter(self):
        self.assertTrue(platform_supported(5))
        self.assertEqual(load_adapter(5).__name__, "myUtils.account_stats.pdd")

    def test_adapter_import_is_relative_so_it_survives_main_startup(self):
        """适配器必须用**相对 import** 取。

        后端既可以 `import myUtils.account_stats`，也可以 `python sau_backend.py`
        直接跑——后者会把本包注册成 `__main__`。以前这里拼的是绝对模块名
        （把 dunder name 和适配器名拼在一起），在那种启动方式下会得到一个不存在的
        模块名，报错还长得像 "base 不是一个包"（实测踩过）。钉住这条，别改回去。
        """
        import inspect

        from myUtils.account_stats import base

        source = inspect.getsource(base.load_adapter)
        self.assertIn("__package__", source)
        # 别再退回"用模块名拼绝对 import 路径"的写法
        self.assertNotIn('f"{__name__}', source)

    def test_missing_cookie_file_is_a_readable_message(self):
        """cookie 文件不在 → 一句人话，而不是 FileNotFoundError。"""
        from myUtils.account_stats import tencent

        with self.assertRaises(StatsUnavailable) as ctx:
            tencent.fetch("__不存在的文件__.json", "测试账号")
        self.assertIn("测试账号", str(ctx.exception))


# --------------------------------------------------------------------------- #
# 小红书解析
# --------------------------------------------------------------------------- #


class XhsParsingTest(unittest.TestCase):
    def test_fans_section_separates_gained_lost_and_net(self):
        """涨粉 / 掉粉 / 净涨粉是三个不同的数，混成一个就说不清账号到底在涨还是在掉。"""
        personal = {"fans_count": 21, "follow_count": 10, "faved_count": 425}
        base = {
            "seven": {
                "rise_fans_count": 16,
                "loss_fans_count": 1,
                "net_rise_fans_count": 16,
                "rise_fans_count_rate": 300,
                "rise_fans_list": [
                    {"date": 1790697600000, "count": 2},
                    {"date": 1790611200000, "count": 1},
                ],
                "loss_fans_list": [],
            }
        }
        fans = _fans_section(personal, base)
        self.assertEqual(fans["count"], 21)
        self.assertEqual(fans["following_count"], 10)
        self.assertEqual(fans["likes_total"], 425)
        period = fans["periods"]["7d"]
        self.assertEqual(period["gained"], 16)
        self.assertEqual(period["lost"], 1)
        self.assertEqual(period["net"], 16)
        self.assertEqual(period["gain_rate"], 300)
        # 空的掉粉序列留成空列表（前端据此不画图），不能塞一个 0 进去
        self.assertEqual(period["loss_series"], [])

    def test_series_is_sorted_ascending(self):
        """平台给的是倒序（最近在前），趋势图要正序——不翻过来图会左右颠倒。"""
        base = {
            "seven": {
                "rise_fans_list": [
                    {"date": 1790697600000, "count": 5},
                    {"date": 1790524800000, "count": 1},
                    {"date": 1790611200000, "count": 3},
                ]
            }
        }
        series = _fans_section({}, base)["periods"]["7d"]["rise_series"]
        self.assertEqual([row["count"] for row in series], [1, 3, 5])
        self.assertEqual(series[0]["date"], "2026-09-28")
        self.assertEqual(series[-1]["date"], "2026-09-30")

    def test_media_item_maps_platform_field_names(self):
        note = {
            "id": "6abcd35e000000001402dfb7",
            "display_title": "想起了青铜门的故事",
            "type": "video",
            "time": "2026-09-30 17:16",
            "likes": 0,
            "view_count": 12,
            "collected_count": 3,
            "comments_count": 1,
            "shared_count": 2,
            "video_info": {"duration": 31},
            "images_list": [{"url": "http://example.test/cover.jpg"}],
        }
        item = _media_item(note)
        self.assertEqual(item["title"], "想起了青铜门的故事")
        self.assertEqual(item["views"], 12)
        self.assertEqual(item["collects"], 3)
        self.assertEqual(item["comments"], 1)
        self.assertEqual(item["shares"], 2)
        self.assertEqual(item["duration"], 31)
        self.assertIn("6abcd35e000000001402dfb7", item["url"])


# --------------------------------------------------------------------------- #
# 抖音解析（形状取自真实响应）
# --------------------------------------------------------------------------- #


class DouyinParsingTest(unittest.TestCase):
    """抖音把数字给成**字符串**，而且增粉 / 掉粉是分开的两个 key。"""

    def test_overview_derives_net_fans(self):
        data = {
            "play": {
                "current_count": "188",
                "option_list": [
                    {"count": "96", "date": "2026-09-26"},
                    {"count": "51", "date": "2026-09-30"},
                ],
            },
            "digg": {"current_count": "13"},
            "new_fans": {"current_count": "7", "option_list": [{"count": "4", "date": "2026-09-30"}]},
            "cancel_fans": {"current_count": "2", "option_list": [{"count": "1", "date": "2026-09-30"}]},
        }
        period = douyin_period(data, "7d")
        self.assertEqual(period["view_count"], 188)
        self.assertEqual(period["like_count"], 13)
        self.assertEqual(period["rise_fans_count"], 7)
        self.assertEqual(period["loss_fans_count"], 2)
        # 净增 = 新增 − 取关，和小红书 / 快手口径一致
        self.assertEqual(period["net_rise_fans_count"], 5)
        # 原始的 new_fans / cancel_fans 要删掉，否则前端看到两套同义字段
        self.assertNotIn("new_fans", period)
        self.assertNotIn("cancel_fans", period)
        # 明细里也要自己算净增（4 − 1 = 3）
        net = period["series"]["net_rise_fans_count"]
        self.assertEqual(net, [{"date": "2026-09-30", "count": 3}])

    def test_series_is_sorted_ascending(self):
        """抖音的逐日明细顺序是乱的，必须自己排。"""
        data = {
            "play": {
                "current_count": "3",
                "option_list": [
                    {"count": "1", "date": "2026-09-28"},
                    {"count": "2", "date": "2026-09-26"},
                    {"count": "3", "date": "2026-09-30"},
                ],
            }
        }
        series = douyin_period(data, "7d")["series"]["view_count"]
        self.assertEqual(
            [row["date"] for row in series], ["2026-09-26", "2026-09-28", "2026-09-30"]
        )

    def test_string_counts_do_not_leak_as_strings(self):
        period = douyin_period({"play": {"current_count": "188"}}, "7d")
        self.assertIsInstance(period["view_count"], int)

    def test_empty_values_become_none_not_zero(self):
        """`""` 是"平台没给"，不是 0。"""
        period = douyin_period({"play": {"current_count": ""}}, "7d")
        self.assertNotIn("view_count", period)

    def test_item_maps_metrics(self):
        item = douyin_item(
            {
                "id": 7690883466161835318,
                "create_time": 1790673353,
                "description": "雪山峡谷间的盘山公路行车实拍",
                "video_info": {"duration": 30279},
                "cover": {"url_list": ["https://example.test/c.webp"]},
                "metrics": {
                    "view_count": "40",
                    "like_count": "7",
                    "favorite_count": "2",
                    "comment_count": "1",
                    "share_count": "3",
                    "subscribe_count": "4",
                    "completion_rate": "0.117647",
                },
                "type": 2,
            }
        )
        self.assertEqual(item["views"], 40)
        self.assertEqual(item["likes"], 7)
        self.assertEqual(item["collects"], 2)
        self.assertEqual(item["comments"], 1)
        self.assertEqual(item["shares"], 3)
        self.assertEqual(item["rise_fans"], 4)
        # 毫秒时长 → 秒
        self.assertEqual(item["duration"], 30)
        # 完播率 0~1 → 百分数
        self.assertEqual(item["full_view_rate"], 11.76)
        self.assertTrue(item["published_at"])


# --------------------------------------------------------------------------- #
# 快手解析
# --------------------------------------------------------------------------- #


class KuaishouParsingTest(unittest.TestCase):
    def test_overview_tabs_map_to_canonical_fields(self):
        data = {
            "basicData": [
                {"tab": "PLAY", "sumCount": 6763.0, "trendData": [{"date": "20260930", "count": 4719.0}]},
                {"tab": "LIKE", "sumCount": 18.0},
                {"tab": "PURE_INCREASE_FAN", "sumCount": 6.0},
                {"tab": "COMMENT", "sumCount": 1.0},
                {"tab": "SHARE", "sumCount": 0.0},
                # 快手给的是 0~1 的小数，要换算成百分数（小红书同名字段就是百分数）
                {"tab": "COMPLETE_RATIO", "sumCount": 0.0432},
                {"tab": "UNKNOWN_TAB", "sumCount": 999},
            ]
        }
        period = ks_period(data)
        self.assertEqual(period["view_count"], 6763)
        self.assertEqual(period["like_count"], 18)
        self.assertEqual(period["net_rise_fans_count"], 6)
        self.assertEqual(period["comment_count"], 1)
        self.assertEqual(period["share_count"], 0)
        self.assertEqual(period["video_full_view_rate"], 4.32)
        self.assertNotIn("UNKNOWN_TAB", json.dumps(period))
        # 日期从 "20260930" 转成 "2026-09-30"
        self.assertEqual(period["series"]["view_count"][0]["date"], "2026-09-30")

    def test_photo_item_has_no_share_field(self):
        """快手没有作品级分享数——字段必须**不存在**，而不是 0。"""
        item = ks_item(
            {
                "photoId": "3r589kw62svu9a",
                "title": "大兴安岭的秋天",
                "publishTime": 1790760223541,
                "playCount": 1019,
                "likeCount": 2,
                "commentCount": 1,
                "collectCount": 0,
                "followCount": 0,
                "duration": 70666,
                "fpr": 0.25,
            }
        )
        self.assertEqual(item["views"], 1019)
        self.assertEqual(item["collects"], 0)
        self.assertEqual(item["rise_fans"], 0)
        self.assertEqual(item["full_view_rate"], 25.0)
        self.assertNotIn("shares", item)


# --------------------------------------------------------------------------- #
# 视频号解析（字段名来自平台自己的前端 bundle；当前账号登出，无真实响应）
# --------------------------------------------------------------------------- #


class TencentParsingTest(unittest.TestCase):
    def test_daily_series_aligns_dates_to_the_window(self):
        """视频号给的是**平行数组**，日期得按 startTs + i 天自己推。"""
        series = _daily_series([1, 0, 2], start_ts=1783000000, days=3)
        self.assertEqual([row["count"] for row in series], [1, 0, 2])
        self.assertEqual(len({row["date"] for row in series}), 3)
        self.assertEqual(series[0]["date"], "2026-07-02")
        self.assertEqual(series[-1]["date"], "2026-07-04")

    def test_daily_series_keeps_the_extra_bucket_for_today(self):
        """要 7 天，平台返回 **8** 段（含今天）——不能按 7 截断。

        实测：`interval=3` 是按天、首尾都算在内，30 天给 31 段，而 7 天那份就是 30 天那份的
        最后 8 个。按 `days` 截断会正好把"今天"丢掉，也就是刚发完视频的那天。
        """
        week = [36, 30, 69, 20, 17, 13, 17, 1]
        series = _daily_series(week, start_ts=1783000000, days=7)
        self.assertEqual([row["count"] for row in series], week)
        self.assertEqual(len(series), 8)

        # 30 天的最后 8 段和 7 天那份逐位相同（这就是"按天、含首尾"的证据）
        month = [0] * 23 + week
        month_series = _daily_series(month, start_ts=1783000000, days=30)
        self.assertEqual([row["count"] for row in month_series][-8:], week)

        # 多到离谱的响应（平台改了口径）还是要挡住，别画出一堆没有意义的点
        self.assertEqual(len(_daily_series([1] * 500, start_ts=1783000000, days=7)), 8)

    def test_daily_series_accepts_object_shape_too(self):
        """对象形状保留兼容（真实响应是纯数组，见上一条）。"""
        series = _daily_series(
            [{"count": 5}, {"value": 6}, {"date": "2026-09-30", "count": 7}],
            start_ts=1783000000,
            days=3,
        )
        self.assertEqual([row["count"] for row in series], [5, 6, 7])
        self.assertEqual(series[-1]["date"], "2026-09-30")

    def test_total_data_uses_the_summary_endpoint_short_keys(self):
        """账号级汇总是**短名**（browse/like/comment/forward/fav），用长名一个都对不上。

        真实响应（2026-10-09 实测，`statistic/post_total_data`，7 天）：
        `{"browse": [36,30,69,20,17,13,17,1], "like": [...], "comment": [...],
          "forward": [...], "fav": [...]}`
        照前端 bundle 那套长名（`readCount` / `likeCount`…）去认，互动那一整块都是空的，
        界面看着像"这个号没有任何互动"。
        """
        real = {
            "browse": [36, 30, 69, 20, 17, 13, 17, 1],
            "like": [0, 0, 0, 0, 0, 0, 0, 0],
            "comment": [1, 0, 0, 0, 0, 0, 0, 0],
            "forward": [2, 0, 0, 1, 0, 0, 0, 0],
            "fav": [0, 0, 0, 0, 0, 0, 0, 0],
        }
        period = _period_from_total_data(real, start_ts=1783000000, days=7)
        self.assertEqual(period["view_count"], sum(real["browse"]))
        self.assertEqual(period["like_count"], 0)
        self.assertEqual(period["comment_count"], 1)
        self.assertEqual(period["share_count"], 3)
        self.assertEqual(period["collect_count"], 0)
        self.assertEqual(len(period["series"]["view_count"]), 8)

    def test_total_data_sums_daily_values(self):
        period = _period_from_total_data(
            {"readCount": [10, 20, 30], "likeCount": [1, 1, 1], "favCount": [0, 0, 2]},
            start_ts=1783000000,
            days=3,
        )
        # 总量用逐日求和，保证和趋势图对得上
        self.assertEqual(period["view_count"], 60)
        self.assertEqual(period["like_count"], 3)
        self.assertEqual(period["collect_count"], 2)
        self.assertEqual(len(period["series"]["view_count"]), 3)

    def test_full_play_rate_is_not_summed(self):
        """完播率是比率，求和会得到荒谬的数字（三个 50% 加起来是 150%）。"""
        period = _period_from_total_data(
            {"fullPlayRate": [0.5, 0.4, 0.25]}, start_ts=1783000000, days=3
        )
        self.assertEqual(period["video_full_view_rate"], 25.0)
        # 但趋势图还是要画出来的
        self.assertEqual(len(period["series"]["video_full_view_rate"]), 3)

    def test_account_level_summary_has_no_fans_or_completion_rate(self):
        """账号级汇总**不给**涨粉数和完播率 → 这两个 key 缺就是缺，不能变成 0。

        （作品列表里那一行才有 `followCount` / `fullPlayRate`，见下一条。）
        """
        period = _period_from_total_data(
            {"browse": [1, 2], "like": [0, 0]}, start_ts=1783000000, days=1
        )
        self.assertNotIn("rise_fans_count", period)
        self.assertNotIn("video_full_view_rate", period)

    def test_avatar_url_gets_the_size_suffix_wechat_cdn_requires(self):
        """微信头像地址不补尺寸后缀 → CDN 直接 400（实测），本地就永远没有头像。

        实测：`https://wx.qlogo.cn/mmhead/ver_1/<hash>` 返回 400 且响应体为空；
        加 `/0`（原图，约 30KB）或 `/132` 才是 200 + `image/jpeg`。
        """
        from myUtils.account_stats.tencent import _avatar_url

        base = "https://wx.qlogo.cn/mmhead/ver_1/Wia51SRXS9t1S39"
        self.assertEqual(_avatar_url(base), base + "/0")
        # 已经带尺寸的不要重复加
        self.assertEqual(_avatar_url(base + "/132"), base + "/132")
        self.assertEqual(_avatar_url(base + "/0?x=1"), base + "/0?x=1")
        # 其它平台的 CDN 没这个规矩，一个字符都别动
        xhs = "https://sns-avatar-qc.xhscdn.com/avatar/abc123"
        self.assertEqual(_avatar_url(xhs), xhs)
        self.assertIsNone(_avatar_url(None))

    def test_post_item_maps_wechat_channels_field_names(self):
        item = _post_item(
            {
                "objectId": "abc123",
                "createTime": 1790673353,
                "desc": {"description": "秋天的第一场雪"},
                "readCount": 100,
                "likeCount": 7,
                "favCount": 3,
                "commentCount": 2,
                "forwardAggregationCount": 1,
                "followCount": 4,
                "fullPlayRate": 0.5,
            }
        )
        self.assertEqual(item["views"], 100)
        self.assertEqual(item["likes"], 7)
        self.assertEqual(item["collects"], 3)
        self.assertEqual(item["shares"], 1)
        self.assertEqual(item["rise_fans"], 4)
        self.assertEqual(item["full_view_rate"], 50.0)


# --------------------------------------------------------------------------- #
# 视频号取数用的**是哪个浏览器 profile**
# --------------------------------------------------------------------------- #


class TencentSessionProfileTest(unittest.TestCase):
    """视频号取数必须落在**账号自己的**持久化 profile 上（`uin_<微信号>`）。

    回归 2026-10-09 的真实事故：适配器传 `seed=False`（本意是"别拿 cookie 文件里那条可能
    已经死掉的 sessionid 去顶 profile"），而 session 层里 `seed=False` 的含义是"**用一个全新
    的临时 profile**"——于是每次取数都在 `_login_<随机>` 的空白 profile 里开浏览器，里面一条
    登录态都没有，数据中心整页跳登录页。界面上的表现极具误导性：

    * 同一个账号，10:00:34 / 10:01:23 / 10:03:33 三次 `cookie_auth` 全是「cookie 有效」
      （它走的是 `uin_<微信号>` 那个 profile），10:00:43 的取数却报"未登录状态"；
    * 用户按提示反复重新扫码，扫多少次都没用——因为问题不在账号，在取数拿错了 profile；
    * 每次取数还顺手留下一个 ~18MB 的 `_login_*` 目录（一天攒了 15 个）。
    """

    UIN = "3431959778"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)

        # profile 根目录换到临时目录：别在仓库的 cookies/ 下留垃圾。
        # `profile_root()` 每次调用都读这个模块级常量，所以 patch 生效。
        self.profile_root = root / "tencent_uploader"
        for target, value in (
            ("uploader.tencent_uploader.session.TENCENT_PROFILE_ROOT", self.profile_root),
            ("myUtils.account_stats.tencent.BASE_DIR", root),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        # 账号文件按适配器的约定放在 <BASE_DIR>/cookiesFile/ 下。
        # 内容和真实文件同构：视频号的账号 JSON 里只有 channels 域的两条 cookie。
        cookies_dir = root / "cookiesFile"
        cookies_dir.mkdir()
        self.account_file = cookies_dir / "acct.json"
        self.account_file.write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": "sessionid",
                            "value": "s",
                            "domain": ".channels.weixin.qq.com",
                            "path": "/",
                        },
                        {
                            "name": "wxuin",
                            "value": self.UIN,
                            "domain": ".channels.weixin.qq.com",
                            "path": "/",
                        },
                    ],
                    "origins": [],
                }
            ),
            encoding="utf-8",
        )

        self.launched: dict = {}

    # --- 替身：只记录"用了哪个 profile"，不开真浏览器 ------------------------- #

    def _fake_playwright(self):
        """假的 patchright：把 `launch_persistent_context` 拿到的目录记下来。

        页面内的请求按**真实抓到的响应形状**回放（2026-10-09 从 `channels.weixin.qq.com`
        抄下来的，只截短了数组）——这样"字段名对不对"能在单元测试里钉住，
        不必每次真开浏览器去核。
        """
        launched = self.launched

        responses = {
            # 实测：7 天给 8 段（含今天），total 是逐日累计
            "/statistic/fans_trend": {
                "add": [0, 0, 0, 0, 0, 0, 0, 0],
                "reduce": [0, 0, 0, 0, 0, 0, 0, 0],
                "netAdd": [0, 0, 0, 0, 0, 0, 0, 0],
                "total": [13, 13, 13, 13, 13, 13, 13, 13],
            },
            # 实测：账号级汇总用的是短名
            "/statistic/post_total_data": {
                "browse": [36, 30, 69, 20, 17, 13, 17, 1],
                "like": [0, 0, 0, 0, 0, 0, 0, 0],
                "comment": [1, 0, 0, 0, 0, 0, 0, 0],
                "forward": [2, 0, 0, 1, 0, 0, 0, 0],
                "fav": [0, 0, 0, 0, 0, 0, 0, 0],
            },
            # 实测：作品行确实用长名
            "/statistic/post_list": {
                "list": [
                    {
                        "objectId": "abc123",
                        "createTime": 1790673353,
                        "desc": {"description": "城市海岸线的日落"},
                        "readCount": 7,
                        "likeCount": 1,
                        "favCount": 2,
                        "commentCount": 0,
                        "forwardAggregationCount": 0,
                        "followCount": 0,
                        "fullPlayRate": 0.5,
                    }
                ],
                "totalCount": 45,
            },
            # 实测：昵称 / 头像在 userAttr 里，不在顶层
            "/auth/auth_wx": {
                "displayWordingList": [],
                "userAttr": {
                    "nickname": "麻辣香锅",
                    "encryptedUsername": "AOA****888",
                    "encryptedHeadImage": "https://wx.qlogo.cn/mmhead/ver_1/abc",
                },
                "finderLoginStatus": 1,
            },
        }

        class FakePage:
            url = "about:blank"

            async def goto(self, url, **kwargs):
                self.url = url

            async def wait_for_timeout(self, ms):
                return None

            async def evaluate(self, expression, arg=None):
                # 一个参数的那次是"页面在不在登录页"，页面正常 → False；
                # 两个参数的是页面内 fetch，按上面的表回放。
                if arg is None:
                    return False
                path = arg[0]
                for suffix, data in responses.items():
                    if path.endswith(suffix):
                        return {
                            "ok": True,
                            "body": json.dumps({"errCode": 0, "data": data}),
                        }
                raise AssertionError(f"测试没准备这个接口的响应：{path}")

            async def close(self):
                return None

        class FakeContext:
            async def add_init_script(self, path=None):
                return None

            async def new_page(self):
                return FakePage()

            async def close(self):
                return None

        class FakeChromium:
            async def launch_persistent_context(self, target, **kwargs):
                launched["target"] = str(target)
                return FakeContext()

            async def launch(self, **kwargs):
                # 降级路径（profile 被占用）才会走到这里：本次测试不该出现
                launched["ephemeral"] = True
                raise AssertionError("不该走一次性 context 的降级路径")

        class FakePlaywright:
            chromium = FakeChromium()

        @asynccontextmanager
        async def fake_async_playwright():
            yield FakePlaywright()

        return fake_async_playwright

    def test_stats_fetch_uses_the_accounts_own_profile(self):
        """取数用的是 `uin_<微信号>`，不是 `_login_<随机>` 空 profile。"""
        from myUtils.account_stats import tencent

        with patch("playwright.async_api.async_playwright", self._fake_playwright()):
            tencent.fetch("acct.json", "测试账号")

        self.assertEqual(Path(self.launched["target"]).name, f"uin_{self.UIN}")
        # 一个临时 profile 都不许留下：这正是那次事故的"痕迹"
        self.assertEqual([p.name for p in self.profile_root.glob("_login_*")], [])

    def test_stats_fetch_maps_the_real_response_shapes(self):
        """真实响应形状走一遍完整链路：粉丝 / 互动 / 作品 / 昵称头像都要落到位。"""
        from myUtils.account_stats import tencent

        with patch("playwright.async_api.async_playwright", self._fake_playwright()):
            stats = tencent.fetch("acct.json", "测试账号")

        account = stats["account"]
        # 昵称 / 头像在 auth_wx.userAttr 里；按顶层取就永远是空的。
        # 头像地址还要补上微信 CDN 要求的尺寸后缀（否则下载 400）。
        self.assertEqual(account["nickname"], "麻辣香锅")
        self.assertEqual(account["avatar"], "https://wx.qlogo.cn/mmhead/ver_1/abc/0")

        # 关注者总数取 fans_trend.total 的最后一段
        self.assertEqual(stats["fans"]["count"], 13)

        # 互动：短名要认出来，否则界面像"零互动"
        period = stats["engagement"]["periods"]["7d"]
        self.assertEqual(period["view_count"], sum([36, 30, 69, 20, 17, 13, 17, 1]))
        self.assertEqual(period["comment_count"], 1)
        self.assertEqual(period["share_count"], 3)

        # 作品列表用长名，两套名字各归各的
        media = stats["media"]
        self.assertTrue(media["supported"])
        self.assertEqual(media["total"], 45)
        item = media["items"][0]
        self.assertEqual(item["views"], 7)
        self.assertEqual(item["likes"], 1)
        self.assertEqual(item["collects"], 2)
        self.assertEqual(item["full_view_rate"], 50.0)

    def test_seed_false_with_account_file_and_no_profile_dir_is_refused(self):
        """`account_file` + `seed=False` + 不给 `profile_dir` 是误用，直接拒绝。

        以前这个组合**静默**落到空白 profile 上，看起来像"账号掉登录了"——
        这类"组合出来的分支"必须响亮地失败，否则下次还会有人踩。
        """
        from uploader.tencent_uploader import session as ts

        async def run():
            async with ts.tencent_session(
                object(), self.account_file, seed=False, persist=False
            ):
                pass

        with self.assertRaises(ValueError) as ctx:
            asyncio.run(run())
        self.assertIn("profile_dir", str(ctx.exception))

    def test_missing_account_file_is_reported_without_opening_a_browser(self):
        """账号文件不在 → 一句人话，而且**不为此开一次浏览器**（十几秒白等）。"""
        from myUtils.account_stats import tencent

        with self.assertRaises(StatsUnavailable) as ctx:
            tencent.fetch("没有这个文件.json", "测试账号")

        self.assertIn("测试账号", str(ctx.exception))
        self.assertFalse(self.profile_root.exists(), "不该为一个不存在的账号建 profile 目录")


# --------------------------------------------------------------------------- #
# 缓存
# --------------------------------------------------------------------------- #


@contextmanager
def _db_in_tmpdir():
    """把数据库指到临时文件，别动本机那份真库。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "database.db"
        with patch.object(cache, "DB_PATH", db_path):
            cache.ensure_tables()
            yield db_path


class CacheTest(unittest.TestCase):
    def test_save_and_load_roundtrip(self):
        with _db_in_tmpdir():
            cache.save(7, 1, "我是一棵草", {"fans": {"count": 21}, "fetched_at": "2026-10-01 11:00:00"})
            record = cache.load(7)
        self.assertIsNotNone(record)
        self.assertEqual(record["stats"]["fans"]["count"], 21)
        self.assertEqual(record["account_name"], "我是一棵草")
        self.assertEqual(record["platform_no"], 1)

    def test_save_overwrites_instead_of_appending(self):
        """一个账号只有一行——这是快照，不是时序库。"""
        with _db_in_tmpdir() as db_path:
            cache.save(7, 1, "我是一棵草", {"fetched_at": "2026-10-01 11:00:00"})
            cache.save(7, 1, "我是一棵草", {"fetched_at": "2026-10-01 12:00:00"})
            record = cache.load(7)
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute("SELECT COUNT(*) FROM account_stats").fetchone()[0]
        self.assertEqual(rows, 1)
        self.assertEqual(record["fetched_at"], "2026-10-01 12:00:00")

    def test_unknown_account_returns_none(self):
        with _db_in_tmpdir():
            self.assertIsNone(cache.load(12345))

    def test_freshness_is_conservative(self):
        """读不出时间就当**不新鲜**——把过期数据当新鲜的会误导用户。"""
        with _db_in_tmpdir():
            cache.save(7, 1, "某人", {"fetched_at": "2026-10-01 11:00:00"})
            record = cache.load(7)
        self.assertFalse(cache.is_fresh(record, ttl=1))
        self.assertFalse(cache.is_fresh(None))
        self.assertFalse(cache.is_fresh({"fetched_at": "不是时间"}))

    def test_age_seconds_of_unparsable_timestamp_is_none(self):
        self.assertIsNone(cache.age_seconds(None))
        self.assertIsNone(cache.age_seconds("乱写的"))

    def test_load_many_returns_dict(self):
        with _db_in_tmpdir():
            cache.save(1, 1, "甲", {"fetched_at": "2026-10-01 11:00:00"})
            cache.save(2, 4, "乙", {"fetched_at": "2026-10-01 11:00:00"})
            records = cache.load_many([1, 2, 3])
        self.assertEqual(set(records.keys()), {1, 2})
        self.assertEqual(records[2]["account_name"], "乙")

    def test_delete_removes_row(self):
        """删账号时要把缓存一起删掉，否则 id 复用会串数据。"""
        with _db_in_tmpdir():
            cache.save(7, 1, "某人", {"fetched_at": "2026-10-01 11:00:00"})
            cache.delete(7)
            self.assertIsNone(cache.load(7))

    def test_account_lock_is_per_account_and_stable(self):
        self.assertIs(cache.account_lock(3), cache.account_lock(3))
        self.assertIsNot(cache.account_lock(3), cache.account_lock(4))


# --------------------------------------------------------------------------- #
# 同步（三个入口共用的一层）
# --------------------------------------------------------------------------- #


def _account(account_id=7, platform_no=4, file_name="x.json", name="某人"):
    return {
        "id": account_id,
        "type": platform_no,
        "file_path": file_name,
        "name": name,
        "platform": {1: "小红书", 2: "视频号", 3: "抖音", 4: "快手"}.get(platform_no, ""),
    }


class SyncAccountTest(unittest.TestCase):
    """`sync_account()` 是"抓 + 覆盖本地库"的唯一入口，三个调用方都靠它。

    这里全部**不碰真实平台**：把 `myUtils.account_stats.fetch` 换掉，
    只验证编排——写库时机、失败时保不保留旧数据、结论码怎么映射。
    """

    def test_success_writes_local_db(self):
        stats = {"fans": {"count": 21}, "media": {"supported": True, "items": [1, 2]},
                 "fetched_at": "2026-10-01 13:00:00"}
        with _db_in_tmpdir():
            with patch("myUtils.account_stats.fetch", lambda *a, **k: stats):
                row = stats_sync.sync_account(_account(platform_no=3, file_name="x.json"))
            saved = cache.load(7)
        self.assertEqual(row["status"], stats_sync.STATUS_OK)
        self.assertEqual(row["fans_count"], 21)
        self.assertIn("粉丝 21", row["message"])
        self.assertIn("作品 2", row["message"])
        # 真的落库了（覆盖本地库是这个功能的重点）
        self.assertEqual(saved["stats"]["fans"]["count"], 21)

    def test_failure_keeps_previous_data(self):
        """抓失败**不能**把本地库擦成空壳——那是用一次网络抖动换掉好好的历史数据。"""
        old = {"fans": {"count": 11}, "fetched_at": "2026-10-01 10:00:00"}
        with _db_in_tmpdir():
            cache.save(7, 4, "某人", old)
            with patch("myUtils.account_stats.fetch", side_effect=AccountStatsError("签名失败")):
                row = stats_sync.sync_account(_account())
            still = cache.load(7)
        self.assertEqual(row["status"], stats_sync.STATUS_ERROR)
        self.assertEqual(still["stats"]["fans"]["count"], 11, "旧数据必须留着")

    def test_stats_unavailable_maps_to_invalid_not_error(self):
        """平台拿不到（未登录/不支持）和真正的出错要分开：前者用户扫码就能解决。"""
        with _db_in_tmpdir():
            with patch("myUtils.account_stats.fetch", side_effect=StatsUnavailable("视频号未登录")):
                row = stats_sync.sync_account(_account(platform_no=2))
        self.assertEqual(row["status"], stats_sync.STATUS_INVALID)
        self.assertIn("未登录", row["message"])

    def test_unexpected_exception_does_not_escape(self):
        with _db_in_tmpdir():
            with patch("myUtils.account_stats.fetch", side_effect=RuntimeError("意料之外")):
                row = stats_sync.sync_account(_account(platform_no=3))
        self.assertEqual(row["status"], stats_sync.STATUS_ERROR)
        self.assertIn("意料之外", row["message"])

    def test_missing_id_or_file_is_reported(self):
        row = stats_sync.sync_account({"id": 0, "type": 4, "file_path": ""})
        self.assertEqual(row["status"], stats_sync.STATUS_ERROR)
        self.assertIn("不完整", row["message"])

    def test_browser_lock_timeout_maps_to_busy(self):
        """浏览器名额等不到 → busy（本轮跳过），而不是硬等、也不是报成 error。

        这里卡的是**并发名额**（`browser_slot`）而不是账号锁：`sync_account` 这一层
        只抢名额，账号锁由适配器自己上（见 `sync.py` 的说明）。
        """
        from myUtils import browser_lock
        from myUtils.browser_lock import BrowserLockTimeout

        class NoSlots:
            def acquire(self, blocking=True, timeout=None):
                return False

            def release(self):
                pass

        with _db_in_tmpdir():
            with patch.object(browser_lock, "BROWSER_SLOTS", NoSlots()):
                row = stats_sync.sync_account(_account(platform_no=4), wait_timeout=0.01)
        self.assertEqual(row["status"], stats_sync.STATUS_BUSY)
        self.assertIn("并发名额", row["message"])

    def test_douyin_does_not_take_a_browser_slot(self):
        """抖音是纯 HTTP，不该去抢"整机浏览器名额"——抢了等于白占一个位置。"""
        taken = []

        class FakeSlots:
            def acquire(self, blocking=True, timeout=None):
                taken.append(1)
                return True

            def release(self):
                taken.append(-1)

        stats = {"fans": {"count": 0}, "media": {"supported": True, "items": []},
                 "fetched_at": "2026-10-01 13:00:00"}
        from myUtils import browser_lock

        with _db_in_tmpdir():
            with patch.object(browser_lock, "BROWSER_SLOTS", FakeSlots()):
                with patch("myUtils.account_stats.fetch", lambda *a, **k: stats):
                    stats_sync.sync_account(_account(platform_no=3))
        self.assertEqual(taken, [], "抖音不该占用浏览器名额")


# --------------------------------------------------------------------------- #
# 和会话续期定时任务合流
# --------------------------------------------------------------------------- #


class SessionRenewSyncTest(unittest.TestCase):
    """续期那轮要顺带同步数据，而且**统计要分得清**。

    续期成功 + 同步失败是很常见的组合（视频号那个账号在视频号侧掉线，
    但校验口径仍认为它有效）。分开报，才能一眼看出"登录还在，数据没更新"。
    """

    def test_run_once_reports_sync_counts_separately(self):
        from myUtils import session_renew

        rows = [
            {"id": 1, "name": "甲", "platform": "小红书", "status": session_renew.STATUS_OK,
             "stats": {"status": stats_sync.STATUS_OK, "message": "已同步"}},
            {"id": 2, "name": "乙", "platform": "视频号", "status": session_renew.STATUS_OK,
             "stats": {"status": stats_sync.STATUS_INVALID, "message": "未登录"}},
            {"id": 3, "name": "丙", "platform": "快手", "status": session_renew.STATUS_INVALID},
        ]
        accounts = [{"id": 1}, {"id": 2}, {"id": 3}]
        with patch.object(session_renew, "list_accounts", lambda: accounts), \
             patch.object(session_renew, "renew_account", side_effect=rows) as renew:
            result = session_renew.run_once("测试")
        self.assertEqual(renew.call_count, 3)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["ok"], 2)
        self.assertEqual(result["invalid"], 1)
        # 三个数字要能对上账：ok + invalid + failed == 总账号数
        self.assertEqual(result["stats_ok"], 1)
        self.assertEqual(result["stats_invalid"], 1)
        self.assertEqual(result["stats_failed"], 0)
        # 续期就失效的账号（第 3 个）根本不会走到同步，所以同步数字只覆盖
        # "续期成功的那几个"——差额应当正好等于续期未成功的账号数。
        self.assertEqual(
            result["stats_ok"] + result["stats_invalid"] + result["stats_failed"],
            result["total"] - result["invalid"],
        )

    def test_run_once_can_skip_stats(self):
        from myUtils import session_renew

        captured = {}

        def fake_renew(account, **kwargs):
            captured.update(kwargs)
            return {"id": 1, "status": session_renew.STATUS_OK}

        with patch.object(session_renew, "list_accounts", lambda: [{"id": 1}]), \
             patch.object(session_renew, "renew_account", fake_renew):
            session_renew.run_once("测试", with_stats=False)
        self.assertIs(captured.get("with_stats"), False)

    def _renew(self, *, cookie_ok, sync=None, with_stats=True, tmp=None):
        """用替身跑一次 `renew_account`，返回结果行。

        替掉的是**最外层的两个依赖**：`check_cookie`（会话结论）和 `sync_account`（数据同步）。
        不需要真的开浏览器，也不需要临时目录里真有一个 cookie 文件——那样测的是
        "文件在不在"，不是这里的编排逻辑。
        """
        from myUtils import session_renew

        kwargs = {"with_stats": with_stats}
        with patch.object(session_renew, "COOKIE_DIR", Path(tmp or ".")), \
             patch.object(session_renew, "_mark_invalid", lambda _id: None), \
             patch.object(session_renew, "browser_session", _null_session), \
             patch.object(session_renew, "check_cookie", _async_return(cookie_ok)):
            if sync is not None:
                kwargs["sync_account"] = sync
                with patch.object(session_renew, "sync_account", sync):
                    return session_renew.renew_account(
                        {"id": 1, "type": 1, "file_path": "x.json", "name": "甲"},
                        with_stats=with_stats,
                    )
            return session_renew.renew_account(
                {"id": 1, "type": 1, "file_path": "x.json", "name": "甲"},
                with_stats=with_stats,
            )


    def test_renew_account_skips_sync_when_session_is_dead(self):
        """会话都失效了，同步注定被平台拒——不该再白开一次浏览器。"""
        called = []
        with _cookie_file() as tmp:
            row = self._renew(
                cookie_ok=False,
                sync=lambda account, **kw: called.append(account) or {"status": "ok"},
                tmp=tmp,
            )
        self.assertEqual(row["status"], "invalid")
        self.assertNotIn("stats", row)
        self.assertEqual(called, [], "续期失败时不该触发同步")

    def test_renew_account_attaches_stats_when_session_is_alive(self):
        with _cookie_file() as tmp:
            row = self._renew(
                cookie_ok=True,
                sync=lambda account, **kw: {"status": "ok", "message": "已同步"},
                tmp=tmp,
            )
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["stats"]["status"], "ok")

    def test_sync_failure_does_not_flip_the_renew_conclusion(self):
        """同步炸了不能把"会话有效，已续期"这个结论反转掉。"""

        def boom(account, **kwargs):
            raise RuntimeError("同步炸了")

        with _cookie_file() as tmp:
            row = self._renew(cookie_ok=True, sync=boom, tmp=tmp)
        self.assertEqual(row["status"], "ok", "续期的结论不该被同步的异常改写")
        self.assertEqual(row["stats"]["status"], "error")

    def test_with_stats_false_only_renews(self):
        called = []
        with _cookie_file() as tmp:
            row = self._renew(
                cookie_ok=True,
                sync=lambda account, **kw: called.append(account) or {},
                with_stats=False,
                tmp=tmp,
            )
        self.assertEqual(row["status"], "ok")
        self.assertNotIn("stats", row)
        self.assertEqual(called, [])


@contextmanager
def _cookie_file():
    """临时目录 + 一个空的 cookie 文件（`renew_account` 先判它在不在）。"""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "x.json").write_text("{}", encoding="utf-8")
        yield tmp


@contextmanager
def _null_session(*args, **kwargs):
    """替掉 `browser_session`：只测编排，不去真的抢锁开浏览器。

    必须收 `*args, **kwargs`：真实调用是 `browser_session(path, wait_timeout=0)`，
    签名对不上会抛 `unexpected keyword argument`，而这个异常会被 `renew_account` 的
    `except Exception` 兜成一个 "续期出错"，测试里表现为"结论莫名其妙变成 error"
    （实测踩过：排查了好一会儿才发现是替身签名的问题，不是产品代码）。
    """
    yield


def _async_return(value):
    """造一个 `check_cookie` 替身（它是协程，调用方会 await）。"""

    async def _inner(*args, **kwargs):
        return value

    return _inner


# --------------------------------------------------------------------------- #
# Web 接口
# --------------------------------------------------------------------------- #


class WebLayerTest(unittest.TestCase):
    """`/account-stats` 的编排逻辑：缓存优先、失败保留旧数据。"""

    def setUp(self):
        import sau_backend_stats as module

        self.module = module

    def test_endpoint_is_registered_under_its_own_prefix(self):
        """刻意不挂在 `/xhs` 下——这份数据四个平台都有，不只小红书。"""
        import sau_backend

        rules = {str(rule) for rule in sau_backend.app.url_map.iter_rules()}
        self.assertIn("/account-stats", rules)
        self.assertIn("/account-stats/summary", rules)

    def test_envelope_marks_stale_and_keeps_old_stats_on_failure(self):
        """抓取失败但有旧数据时，**旧数据要一起返回**，而不是把弹窗清空。"""
        record = {"stats": {"fans": {"count": 21}}, "fetched_at": "2026-10-01 11:00:00"}
        envelope = self.module._cache_envelope(
            record, stale=True, fetched=False, error="浏览器打不开"
        )
        self.assertEqual(envelope["stats"]["fans"]["count"], 21)
        self.assertTrue(envelope["stale"])
        self.assertFalse(envelope["fetched"])
        self.assertEqual(envelope["error"], "浏览器打不开")

    def test_envelope_without_cache_has_no_stats(self):
        envelope = self.module._cache_envelope(None, stale=False, fetched=False)
        self.assertIsNone(envelope["stats"])
        self.assertIsNone(envelope["fetched_at"])

    def test_unsupported_platform_is_flagged(self):
        envelope = self.module._cache_envelope(
            None, stale=False, fetched=False, error="这个平台没有接口", unsupported=True
        )
        self.assertTrue(envelope["unsupported"])

    def test_media_limit_defaults(self):
        self.assertEqual(self.module.MEDIA_LIMIT_DEFAULT, 20)
        self.assertEqual(self.module.MEDIA_LIMIT_MAX, 100)

    def test_fetch_blocking_turns_stats_unavailable_into_a_message(self):
        """`StatsUnavailable` 是"平台不支持 / 没登录"，要变成可读说明而不是 500。"""
        account = {"id": 1, "type": 2, "filePath": "x.json", "name": "麻辣香锅"}

        def boom(*args, **kwargs):
            return {"status": stats_sync.STATUS_INVALID, "message": "视频号登录态已失效"}

        with patch.object(self.module.account_stats_sync, "sync_account", boom):
            result = self.module._fetch_blocking(account, True, 10)
        self.assertTrue(result["unsupported"])
        self.assertEqual(result["error"], "视频号登录态已失效")
        self.assertIsNone(result["stats"])

    def test_fetch_blocking_reports_real_errors_separately(self):
        account = {"id": 1, "type": 1, "filePath": "x.json", "name": "甲"}

        def boom(*args, **kwargs):
            return {"status": stats_sync.STATUS_ERROR, "message": "小红书签名失败"}

        with patch.object(self.module.account_stats_sync, "sync_account", boom):
            result = self.module._fetch_blocking(account, True, 10)
        self.assertFalse(result["unsupported"])
        self.assertEqual(result["error"], "小红书签名失败")

    def test_fetch_blocking_swallows_unexpected_exceptions(self):
        """适配器里没预料到的异常不该变成 500 页面。"""
        account = {"id": 1, "type": 3, "filePath": "x.json", "name": "甲"}

        def boom(*args, **kwargs):
            return {"status": stats_sync.STATUS_ERROR, "message": "同步出错：意料之外"}

        with patch.object(self.module.account_stats_sync, "sync_account", boom):
            result = self.module._fetch_blocking(account, True, 10)
        self.assertIn("意料之外", result["error"])

    def test_fetch_blocking_saves_to_cache(self):
        account = {"id": 42, "type": 3, "filePath": "x.json", "name": "甲"}
        stats = {"fans": {"count": 5}, "fetched_at": "2026-10-01 11:00:00"}

        def fake_sync(account_info, **kwargs):
            # 真实的 sync_account 会自己写库；替身要照样写，因为 `_fetch_blocking`
            # 现在是从库里读回结果（保证"界面看到的 == 库里存的"）。
            cache.save(42, 3, "甲", stats)
            return {"status": stats_sync.STATUS_OK, "message": "已同步"}

        with _db_in_tmpdir():
            with patch.object(self.module.account_stats_sync, "sync_account", fake_sync):
                result = self.module._fetch_blocking(account, True, 10)
            self.assertEqual(result["stats"], stats)
            self.assertEqual(cache.load(42)["stats"], stats)


class WebEndpointTest(unittest.TestCase):
    """真的打一遍 `/account-stats`（带鉴权），确认不会抛未捕获异常。

    这里刻意**不碰真实平台**：把 `fetch_stats` 换成假的，只验证 Web 层的编排
    （参数校验、缓存判定、以及"账号不存在"那条分支）。
    """

    def setUp(self):
        import sau_backend

        self.client = sau_backend.app.test_client()
        password = _admin_password()
        if not password:
            self.skipTest("settings.json 里没有后台密码，跳过需要鉴权的接口测试")
        response = self.client.post("/auth/login", json={"username": "admin", "password": password})
        token = (response.get_json() or {}).get("data", {}).get("token")
        if not token:
            self.skipTest("后台登录失败，跳过需要鉴权的接口测试")
        self.headers = {"Authorization": f"Bearer {token}"}

    def test_bad_id_is_rejected(self):
        response = self.client.get("/account-stats?id=abc", headers=self.headers)
        self.assertEqual(response.status_code, 400)

    def test_missing_account_returns_200_with_a_readable_message(self):
        """账号没了的时候刻意不用 404——前端拦截器会把 404 说成"请求地址不存在"，
        在这个场景下完全是误导。"""
        response = self.client.get("/account-stats?id=999999", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertIsNone(data["stats"])
        self.assertIn("账号不存在", data["error"])

    def test_summary_never_triggers_a_fetch(self):
        """列表接口**只读缓存**：一进账号管理页就开 N 个浏览器是不可接受的。"""
        import sau_backend_stats as module

        called = []

        def boom(*args, **kwargs):
            called.append(args)
            raise AssertionError("summary 不该触发抓取")

        with patch.object(module.account_stats_sync, "sync_account", boom):
            response = self.client.get("/account-stats/summary", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(called, [])
        self.assertIsInstance(response.get_json()["data"], dict)


def _admin_password() -> str:
    """读本机 settings.json 里的后台密码（接口测试要带 token 才能过鉴权守卫）。"""
    from conf import BASE_DIR

    path = Path(BASE_DIR) / "settings.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))["auth"]["password"]
    except Exception:
        return ""


if __name__ == "__main__":
    unittest.main()
