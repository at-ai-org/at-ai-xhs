"""小红书工作台「确认发布」的浏览器模式开关（无头 / 有头）。

背景：无头模式跑得快、不弹窗口，但更容易被小红书风控提示（「操作过于频繁」等）。
所以确认弹窗给了两个按钮——默认「确认发布（无头）」，以及「有头浏览器发布」，
后者传 `headless=false`，让上传器在本机弹出真实浏览器窗口跑完全程。

这里只验证 **confirm 接口把模式落到任务上**（状态、消息、发布记录文案），
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


class XhsConfirmHeadlessTest(unittest.TestCase):
    def setUp(self):
        self.client = _make_client()
        self.task_id = "test-confirm-headless"
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

    def test_headed_button_flips_task_to_headed(self):
        """点「有头浏览器发布」→ 任务变成有头、消息里说清楚模式。"""
        resp, _, _ = self._confirm(
            {"title": "测试标题", "desc": "测试正文", "headless": False}
        )
        self.assertEqual(resp.status_code, 200)
        snapshot = xhs._task_snapshot(self.task_id)
        self.assertFalse(snapshot["headless"], "有头发布必须把 headless 置为 False")
        self.assertEqual(snapshot["state"], "queued")
        self.assertIn("有头浏览器", snapshot["message"])
        self.assertEqual(snapshot["record_id"], 4242)

    def test_headless_button_keeps_headless(self):
        """默认按钮（无头）→ 任务仍是无头。"""
        resp, _, _ = self._confirm({"title": "测试标题", "headless": True})
        self.assertEqual(resp.status_code, 200)
        snapshot = xhs._task_snapshot(self.task_id)
        self.assertTrue(snapshot["headless"])
        self.assertIn("无头浏览器", snapshot["message"])

    def test_missing_field_keeps_task_default(self):
        """请求体不带 headless（老前端）→ 沿用提交时的默认值，行为不变。"""
        resp, _, _ = self._confirm({"title": "测试标题"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(xhs._task_snapshot(self.task_id)["headless"])

        # 反向也成立：建任务时就选了有头的，不带字段时不该被无头覆盖
        self._seed_task(headless=False)
        resp, _, _ = self._confirm({"title": "测试标题"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(xhs._task_snapshot(self.task_id)["headless"])

    def test_publish_record_mentions_browser_mode(self):
        """发布记录里带上模式：出了风控提示时能看出这条是怎么跑的。"""
        captured: dict = {}
        with patch.object(xhs.threading, "Thread"), patch.object(
            xhs.records, "insert_record", side_effect=lambda rec: (captured.update(rec), 7)[1]
        ):
            resp = self.client.post(
                f"/xhs/task/{self.task_id}/confirm", json={"title": "测试标题", "headless": False}
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


if __name__ == "__main__":
    unittest.main()
