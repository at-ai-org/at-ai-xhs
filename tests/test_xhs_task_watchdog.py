"""小红书工作台的发布任务看门狗（sau_backend_xhs.py）。

工作台和发布中心是两套任务模型（那边是 state 字段 + 一个任务一个线程，没有通道队列），
所以看门狗要单独适配。但**要解决的问题完全一样**：

发布是阻塞式浏览器自动化，"卡住"既不抛异常也不超时。工作台这边每个任务直接起一个
线程，卡住时那个线程永远收不回来，界面上就永远停在「发布中」——用户既不知道发生了
什么，也判断不出能不能重试。

（工作台生成文案那一侧不用管：`chat_completion` 自带超时，且 `_generate_worker` 的
异常路径一定会落到 `pending_confirm`，不会永久停在中间态。）

这里只测内存状态机，`_finish_record` 被替身挡掉，不碰真实数据库。
"""

import threading
import time
import unittest
from unittest.mock import patch

import sau_backend_publish as BP
import sau_backend_xhs as X


def _make_task(task_id: str, *, state: str = "running", app=None) -> dict:
    task = {
        "task_id": task_id,
        "account": "default",
        "account_file": "/tmp/xhs-watchdog-test.json",
        "video": "/tmp/none.mp4",
        "title": "测试笔记",
        "state": state,
        "message": "有头浏览器已启动，正在执行页面操作",
        "record_id": 0,
        "created_at": time.time(),
        "updated_at": time.time(),
        "finished_at": None,
    }
    if app is not None:
        task["_app"] = app
    return task


class _FakeStep:
    def __init__(self, step: str, status: str, message: str = ""):
        self._d = {"step": step, "status": status, "message": message}

    def to_dict(self):
        return dict(self._d)


class _FakeApp:
    def __init__(self, steps=None):
        self.step_results = steps or []


class XhsWatchdogTestBase(unittest.TestCase):
    TIMEOUT = 0.2

    def setUp(self):
        self.task_id = f"xhs-wd-{time.time_ns()}"
        self.record_calls: list[tuple] = []
        self._patches = [
            patch.object(X, "XHS_TASK_TIMEOUT", self.TIMEOUT),
            # 落库是替身：这一组只验证内存状态机
            patch.object(
                X, "_finish_record", lambda *a, **k: self.record_calls.append((a, k))
            ),
        ]
        for p in self._patches:
            p.start()
        with X._tasks_lock:
            X._tasks[self.task_id] = _make_task(self.task_id)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        with X._tasks_lock:
            X._tasks.pop(self.task_id, None)

    def _task(self):
        with X._tasks_lock:
            return dict(X._tasks[self.task_id])

    def _arm(self, app=None):
        if app is not None:
            with X._tasks_lock:
                X._tasks[self.task_id]["_app"] = app
        with X._tasks_lock:
            snapshot = dict(X._tasks[self.task_id])
        return X._arm_xhs_watchdog(self.task_id, 0, snapshot, [], time.time())

    def _wait_done(self, seconds=2.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._task()["state"] not in X.XHS_ACTIVE_STATES:
                return True
            time.sleep(0.02)
        return False


class FiringTest(XhsWatchdogTestBase):
    def test_stuck_task_marked_failed(self):
        timer = self._arm()
        self.assertTrue(self._wait_done(), "看门狗应当把卡住的任务判成失败")
        task = self._task()
        self.assertEqual(task["state"], "failed")
        self.assertIn("超时", task["message"])
        self.assertIsNotNone(task["finished_at"])
        X._disarm_xhs_watchdog(timer)

    def test_message_says_what_to_do(self):
        """失败原因要能指导用户（这类卡住重启后端才能回收资源）。"""
        self._arm()
        self.assertTrue(self._wait_done())
        self.assertIn("重启后端", self._task()["message"])

    def test_publish_record_is_closed_too(self):
        """任务和发布记录必须一起收尾，否则界面上两处各说各话。"""
        self._arm()
        self.assertTrue(self._wait_done())
        self.assertEqual(len(self.record_calls), 1, "应当写一次发布记录")

    def test_steps_are_carried_over(self):
        """已经跑完的步骤要带上，用户能看到卡在哪一步之前。"""
        app = _FakeApp([_FakeStep("open_publish_page", "ok"), _FakeStep("upload_video", "ok")])
        self._arm(app=app)
        self.assertTrue(self._wait_done())
        self.assertEqual(len(self._task()["steps"]), 2)

    def test_pending_confirm_is_not_touched(self):
        """还没确认发布的任务不是"在跑"，看门狗不该碰它。"""
        with X._tasks_lock:
            X._tasks[self.task_id]["state"] = "pending_confirm"
        self._arm()
        time.sleep(self.TIMEOUT * 3)
        self.assertEqual(self._task()["state"], "pending_confirm")

    def test_finished_task_is_not_touched(self):
        with X._tasks_lock:
            X._tasks[self.task_id].update(state="success", message="完成")
        self._arm()
        time.sleep(self.TIMEOUT * 3)
        task = self._task()
        self.assertEqual(task["state"], "success")
        self.assertEqual(task["message"], "完成")

    def test_disarm_prevents_firing(self):
        timer = self._arm()
        X._disarm_xhs_watchdog(timer)
        time.sleep(self.TIMEOUT * 3)
        self.assertEqual(self._task()["state"], "running")

    def test_disarm_tolerates_none(self):
        X._disarm_xhs_watchdog(None)  # 不应抛异常

    def test_unknown_task_is_ignored(self):
        X._arm_xhs_watchdog("no-such-task", 0, {}, [], time.time())
        time.sleep(self.TIMEOUT * 3)  # 不应抛异常


class LateFinishCannotOverwriteTest(XhsWatchdogTestBase):
    def test_late_success_does_not_rewrite_timeout_verdict(self):
        """核心回归：卡住的线程哪天真返回时，不能把"超时失败"改成"成功"。

        那是最坏的一种错——用户会以为发上去了，实际可能压根没发。
        """
        self._arm()
        self.assertTrue(self._wait_done())
        self.assertEqual(self._task()["state"], "failed")

        # 模拟卡住的线程终于跑完，回调了正常收尾
        wrote = X._task_finish(self.task_id, state="success", message="完成")

        self.assertFalse(wrote, "迟到的收尾必须被拒绝")
        task = self._task()
        self.assertEqual(task["state"], "failed", "不许被改写")
        self.assertIn("超时", task["message"])
        self.assertEqual(len(self.record_calls), 1, "迟到的收尾不该再写一次发布记录")

    def test_task_finish_reports_true_normally(self):
        """没有看门狗插过手时，收尾必须照常生效（返回 True）。"""
        self.assertTrue(X._task_finish(self.task_id, state="success", message="完成"))
        self.assertEqual(self._task()["state"], "success")

    def test_task_finish_returns_false_for_unknown_task(self):
        self.assertFalse(X._task_finish("no-such-task", state="failed", message="x"))

    def test_stuck_flag_is_hidden_from_frontend(self):
        """`_stuck` 是内部标记，`_task_snapshot` 必须把它过滤掉。"""
        self._arm()
        self.assertTrue(self._wait_done())
        snapshot = X._task_snapshot(self.task_id)
        self.assertTrue(any(k.startswith("_") is False for k in snapshot))
        self.assertNotIn("_stuck", snapshot)


class TimeoutConfigTest(unittest.TestCase):
    def test_same_knob_as_publish_center(self):
        """两边必须是同一个上限。

        用户在界面上看到的是同一件事（"一条笔记发布中"），不该因为走的是哪个入口
        而超时行为不同。
        """
        self.assertEqual(X.XHS_TASK_TIMEOUT, BP.PUBLISH_TASK_TIMEOUT)

    def test_default_is_thirty_minutes(self):
        self.assertEqual(X.XHS_TASK_TIMEOUT, 30 * 60)

    def test_active_states(self):
        self.assertEqual(X.XHS_ACTIVE_STATES, ("queued", "running"))

    def test_watchdog_is_a_daemon_thread(self):
        """看门狗必须是 daemon，否则会拖住进程退出。"""
        captured = {}

        class RecordingTimer(threading.Timer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                captured["timer"] = self

        with patch.object(X.threading, "Timer", RecordingTimer):
            timer = X._arm_xhs_watchdog("cfg-test", 0, {}, [], time.time())
        self.assertTrue(captured["timer"].daemon)
        timer.cancel()


if __name__ == "__main__":
    unittest.main()
