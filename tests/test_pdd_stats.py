# -*- coding: utf-8 -*-
"""拼多多视频账号数据适配器的解析契约（at-ai-xhs 改造新增）。

抓取那一段要开浏览器、依赖平台在线，不适合进单元测试；这里测的是**解析**，
而夹具是**真机抓到的响应形状**（2026-10-09 用真实账号从创作者后台截下来的），
字段名和值都照抄，只把账号名之类替换掉。

要钉住的三件事：

1. **粉丝数是字符串**（`"1"`，甚至 `"1粉丝"`）→ 必须变成 int，否则界面上是 `"1"`；
2. **成功信封里 `errorCode` 也是非空的**（1000000）——判读只看 `success`/`errorMsg`，
   这个坑在发布链路上已经吃过一次（把成功记成失败）；
3. **"平台没给"和"真的是 0"要分开**：作品级 `shares` 经常是 `null`，
   硬写成 0 会让人以为这条作品真的一条都没分享。
"""

import unittest

from myUtils.account_stats import pdd


#: 真机抓到的 `/sprite/obs/anchor/info` 响应（2026-10-09）。
ANCHOR_INFO = {
    "success": True,
    "errorCode": 1000000,
    "errorMsg": None,
    "result": {
        "roomId": "605276855",
        "name": "林心魂",
        "image": "https://avatar3.pddpic.com/a/xxx",
        "fansNumTip": "1粉丝",
        "fansNumDataTip": "1",
        "fansNumSuffixTip": "粉丝",
        "anchorUid": 7066069890,
        "userCertificationStatus": 1,
    },
}

#: 真机抓到的 `/api/backbone/anchor/query` 响应（字段裁剪到用得到的部分）。
ANCHOR_QUERY = {
    "success": True,
    "errorCode": 1000000,
    "result": {"anchorUid": 7066069890, "anchorName": "林心魂", "anchorIcon": "https://avatar3.pddpic.com/a/xxx", "roomId": 605276855},
}

#: 真机抓到的 `/api/backbone/video/statistic/view` 响应。
STATISTIC = {
    "success": True,
    "errorCode": 1000000,
    "result": {
        "overallStatistic": [
            {"title": "视频发布量", "value": 5},
            {"title": "视频播放量", "value": 1},
            {"title": "视频点赞数", "value": 0},
            {"title": "视频分享量", "value": 0},
            {"title": "视频评论数", "value": 0},
            {"title": "粉丝净增值", "value": 0},
        ],
        "daysStatistic": [
            {
                "title": "播放量",
                "sevenDaysAdd": 1,
                "lastSevenDaysAdd": 0,
                "oneMonthAdd": 1,
                "dayValues": [
                    {"date": "2026-10-07", "value": 0},
                    {"date": "2026-10-08", "value": 1},
                ],
            },
            {
                "title": "视频点赞",
                "sevenDaysAdd": 0,
                "lastSevenDaysAdd": 0,
                "oneMonthAdd": 0,
                "dayValues": [{"date": "2026-10-08", "value": 0}],
            },
        ],
    },
}

#: 真机抓到的 `/api/backbone/video/query` 响应（两条作品：一条数据齐全、一条只有部分）。
FEEDS = {
    "success": True,
    "errorCode": 1000000,
    "result": {
        "feeds": [
            {
                "feedId": "7021734988802412372",
                "title": "",
                "desc": "瑞士山地火车沿线，湖景与木屋村落\n瑞士阿尔卑斯山区的山地火车沿坡下行",
                "coverUrl": "https://video-snapshot01.pddpic.com/back/xxx",
                "vv": 12,
                "likes": 3,
                "comments": 1,
                "shares": 2,
                "publishTime": 1791508378540,
                "isTop": False,
            },
            {
                "feedId": "7021734706894602432",
                "title": "",
                "desc": "云雾青山下的廊桥与湿地湖景",
                "coverUrl": "https://video-snapshot01.pddpic.com/back/yyy",
                "vv": 0,
                "likes": None,
                "comments": 0,
                "shares": None,
                "publishTime": 1791508378540,
            },
        ],
        "total": 23,
        "maxAllowed": 23,
    },
}


class PddStatsParseTests(unittest.TestCase):
    def _build(self, **overrides):
        captured = {
            "anchorInfo": ANCHOR_INFO,
            "anchorQuery": ANCHOR_QUERY,
            "statistic": STATISTIC,
            "feeds": FEEDS,
        }
        captured.update(overrides)
        notes: list[str] = []
        stats = pdd._build(captured, "麻辣腐竹", True, 20, notes)
        return stats, notes

    def test_account_identity_and_fans(self):
        stats, _ = self._build()
        self.assertEqual(stats["account"]["nickname"], "林心魂")
        self.assertEqual(stats["account"]["account_id"], "7066069890")
        self.assertEqual(stats["account"]["platform_name"], "拼多多视频")
        # 粉丝数平台给的是字符串 "1"
        self.assertEqual(stats["fans"]["count"], 1)
        self.assertIsInstance(stats["fans"]["count"], int)

    def test_overall_statistic_is_mapped_by_title(self):
        stats, _ = self._build()
        lifetime = stats["engagement"]["lifetime"]
        self.assertEqual(lifetime["published"], 5)
        self.assertEqual(lifetime["views"], 1)
        self.assertEqual(lifetime["net_rise_fans"], 0)

    def test_periods_and_daily_series(self):
        stats, _ = self._build()
        periods = stats["engagement"]["periods"]
        self.assertEqual(periods["7d"]["views"], 1)
        self.assertEqual(periods["30d"]["views"], 1)
        series = stats["engagement"]["series"]["views"]
        self.assertEqual(len(series), 2)
        self.assertEqual(series[-1], {"date": "2026-10-08", "value": 1})

    def test_media_items_and_pagination(self):
        stats, notes = self._build()
        media = stats["media"]
        self.assertTrue(media["supported"])
        self.assertEqual(media["total"], 23)
        self.assertTrue(media["has_more"])
        self.assertEqual(len(media["items"]), 2)
        first = media["items"][0]
        self.assertEqual(first["title"], "瑞士山地火车沿线，湖景与木屋村落")  # 取描述首行
        self.assertEqual(first["views"], 12)
        self.assertEqual(first["likes"], 3)
        self.assertEqual(first["shares"], 2)
        self.assertTrue(first["published_at"].startswith("2026-"))
        self.assertTrue(any("只取了最近" in note for note in notes))

    def test_missing_metric_is_not_written_as_zero(self):
        """平台没给的数据（`shares: null`）不能写成 0。"""
        stats, _ = self._build()
        second = stats["media"]["items"][1]
        self.assertNotIn("shares", second, "shares=null 被写成了 0，会让人以为真的一条都没分享")
        self.assertEqual(second["views"], 0)  # 这个是真的 0（vv=0）

    def test_error_envelope_with_success_false_is_ignored(self):
        """`success=false` 的响应不能被当数据用（成功信封里 errorCode 也是非空的）。"""
        stats, notes = self._build(
            statistic={"success": False, "errorCode": 400010, "errorMsg": "出错了"}
        )
        self.assertEqual(stats["engagement"]["lifetime"], {})
        self.assertTrue(any("粉丝" in n for n in notes) is False)  # 账号信息仍然拿到了

    def test_login_lost_leaves_empty_but_valid_structure(self):
        """什么都抓不到时结构仍然完整（前端不用到处判空），并给出说明。"""
        notes: list[str] = []
        stats = pdd._build({}, "麻辣腐竹", True, 20, notes)
        self.assertEqual(stats["fans"]["count"], None)
        self.assertFalse(stats["media"]["supported"])
        self.assertEqual(stats["media"]["items"], [])
        self.assertTrue(notes)

    def test_without_media_does_not_claim_supported(self):
        notes: list[str] = []
        stats = pdd._build(
            {"anchorInfo": ANCHOR_INFO, "anchorQuery": ANCHOR_QUERY, "statistic": STATISTIC},
            "麻辣腐竹", False, 20, notes,
        )
        self.assertFalse(stats["media"]["supported"])
        self.assertTrue(any("没有取作品列表" in note for note in notes))


class PddStatsHelpersTests(unittest.TestCase):
    def test_to_int_handles_platform_strings(self):
        self.assertEqual(pdd._to_int("1"), 1)
        self.assertEqual(pdd._to_int("1粉丝"), 1)
        self.assertEqual(pdd._to_int(3.0), 3)
        self.assertIsNone(pdd._to_int(None))
        self.assertIsNone(pdd._to_int(""))
        # bool 是 int 的子类，别把 True 当成 1
        self.assertIsNone(pdd._to_int(True))

    def test_ms_to_str(self):
        self.assertEqual(pdd._ms_to_str(0), None)
        self.assertEqual(pdd._ms_to_str(None), None)
        self.assertRegex(pdd._ms_to_str(1791508378540), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_result_of_ignores_error_code_on_success(self):
        self.assertEqual(pdd._result_of(ANCHOR_INFO)["name"], "林心魂")
        self.assertIsNone(pdd._result_of({"success": False, "errorCode": 400010}))
        self.assertIsNone(pdd._result_of(None))

    def test_platform_5_is_supported_now(self):
        from myUtils.account_stats import platform_supported, platform_name

        self.assertTrue(platform_supported(5), "拼多多现在有适配器了")
        self.assertEqual(platform_name(5), "拼多多视频")


if __name__ == "__main__":
    unittest.main()
