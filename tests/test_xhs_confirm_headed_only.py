"""小红书工作台「确认发布」的浏览器模式：**固定有头**。

背景（这次改动的由来）：2026-03-10 小红书官方账号「薯管家」发布《关于打击 AI 托管
运营账号的治理公告》，明确禁止"利用技术手段模拟真人"，并对"与真人操作习惯不符"的
行为特征分档处置（偶发→警告限流 / 全量 AI 代发→封禁）。

原先确认弹窗给了两档：默认「确认发布（无头）」+「有头浏览器发布」。无头发布正是这条
链路上最扎眼的行为特征，所以**直接砍掉无头档**，发布一律有头；前端即使传
`headless=true` 也会被后端覆盖，不留绕过口子。

只读操作（扫码登录 / 自检 / 列合集）不受影响，仍可无头——它们不提交任何内容。

这里只验证 **confirm 接口把模式钉死成有头**（状态、快照字段、消息、发布记录文案），
线程和发布记录写入都被替身挡掉：不开浏览器、不发布、不写库。
"""

import unittest
from unittest.mock import patch

from flask import Flask

import sau_backend_xhs as xhs


def _make_client() -> "Flask":
    app = Flask(__name__)
    app.register_blueprint(xhs.xhs_bp)
    return app.test_client()


class XhsConfirmHeadedOnlyTest(unittest.TestCase):
    def setUp(self):
        self.client = _make_client()
        self.task_id = "test-confirm-headed-only"
        self._seed_task(headless=True)

    def tearDown(self):
        with xhs._tasks_lock:
            xhs._tasks.pop(self.task_id, None)

    # ---- 辅助 ---------------------------------------------------------- #

    def _seed_task(self, headless: bool) -> None:
        """塞一条「待确认」的任务，和 /xhs/publish 建出来的结构保持一致。"""
        with xhs._tasks_lock:
            xhs._tasks[self.task_id] = {
                "id": self.task_id,
                "state": "pending_confirm",
                "stage": "confirm",
                "message": "等待人工确认后发布",
                "account": "default",
                "account_file": "/tmp/at-ai-xhs-test-cookie.json",
                "video": "/tmp/at-ai-xhs-test-video.mp4",
                "video_name": "at-ai-xhs-test-video.mp4",
                "title": "测试标题",
                "desc": "测试正文",
                "tags": [],
                "collection": None,
                "visibility": None,
                "original": False,
                "schedule": None,
                "dry_run": False,
                "scheduled": False,
                "debug": False,
                "headless": headless,
                "created_at": 0,
                "updated_at": 0,
                "steps": [],
            }

    def _confirm(self, body: dict):
        """调 confirm 接口，但把后台发布线程和发布记录写入替掉。"""
        with patch.object(xhs.threading, "Thread") as fake_thread, patch.object(
            xhs, "_open_publish_record", return_value=4242
        ) as fake_record:
            resp = self.client.post(f"/xhs/task/{self.task_id}/confirm", json=body)
        return resp, fake_thread, fake_record

    # ---- 用例 ---------------------------------------------------------- #

    def test_frontend_cannot_ask_for_headless(self):
        """核心回归：前端传 headless=true 也必须被覆盖成有头。"""
        resp, _, _ = self._confirm(
            {"title": "测试标题", "desc": "测试正文", "headless": True}
        )
        self.assertEqual(resp.status_code, 200)
        snapshot = xhs._task_snapshot(self.task_id)
        self.assertFalse(
            snapshot["headless"],
            "发布一律有头：前端传 headless=true 不该被接受（无头是风控主要触发点）",
        )
        self.assertEqual(snapshot["state"], "queued")
        self.assertIn("有头浏览器", snapshot["message"])
        self.assertNotIn("无头", snapshot["message"])
        self.assertEqual(snapshot["record_id"], 4242)

    def test_missing_field_is_still_headed(self):
        """请求体不带 headless（老前端）→ 依然是有头，不会退回旧默认值。"""
        resp, _, _ = self._confirm({"title": "测试标题"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(xhs._task_snapshot(self.task_id)["headless"])

    def test_headless_seeded_task_is_upgraded_to_headed(self):
        """建任务时旧值是无头（历史快照）→ 确认这一刻被升级成有头。"""
        self._seed_task(headless=True)
        self.assertTrue(xhs._task_snapshot(self.task_id)["headless"])

        resp, _, _ = self._confirm({"title": "测试标题", "headless": True})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(xhs._task_snapshot(self.task_id)["headless"])

    def test_publish_record_mentions_headed_mode(self):
        """发布记录里带上模式，便于回溯每条是怎么跑的。"""
        captured: dict = {}
        with patch.object(xhs.threading, "Thread"), patch.object(
            xhs.records, "insert_record", side_effect=lambda rec: (captured.update(rec), 7)[1]
        ):
            resp = self.client.post(
                f"/xhs/task/{self.task_id}/confirm", json={"title": "测试标题", "headless": True}
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured.get("action", "").count("有头浏览器"), 1)
        self.assertIn("有头浏览器", captured.get("message", ""))
        # 记录 id 要回写到任务上，前端才能给出「发布记录」入口
        self.assertEqual(xhs._task_snapshot(self.task_id)["record_id"], 7)

    def test_worker_is_started_after_confirm(self):
        """确认后必须真的把发布线程拉起来（否则任务会一直挂在排队中）。"""
        _, fake_thread, _ = self._confirm({"title": "测试标题", "headless": False})
        self.assertEqual(fake_thread.call_count, 1)
        self.assertEqual(fake_thread.call_args.kwargs.get("args"), (self.task_id,))
        self.assertTrue(fake_thread.call_args.kwargs.get("daemon"))

    def test_confirm_rejects_non_pending_task(self):
        """只有「待确认」能确认发布——顺手确认一下没被这次改动破坏。"""
        with xhs._tasks_lock:
            xhs._tasks[self.task_id]["state"] = "running"
        resp, _, _ = self._confirm({"title": "测试标题", "headless": False})
        self.assertEqual(resp.status_code, 400)

    def test_confirm_still_validates_title_and_schedule(self):
        """守住被这次改动碰过的校验：标题必填、标题 20 字上限、定时格式。"""
        resp, _, _ = self._confirm({"title": "   "})
        self.assertEqual(resp.status_code, 400)

        resp, _, _ = self._confirm({"title": "这个标题明显超过了小红书规定的二十个字的长度上限"})
        self.assertEqual(resp.status_code, 400)

        resp, _, _ = self._confirm({"title": "测试标题", "schedule": "2026/01/01 10:00"})
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
