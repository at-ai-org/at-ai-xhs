"""发布任务看门狗（sau_backend_publish.py）。

由来（真实事故）：发布任务是**阻塞式**浏览器自动化，"卡住"（等一把永不释放的
profile 锁、等一个不再响应的浏览器驱动）既不抛异常也不超时——任务会永远停在
「发布中」，批次也永远不 done，界面上就是"一直卡着"。当时视频号任务卡了十几分钟
没有任何动静，同批次的抖音/快手早就发完了。

看门狗**杀不掉**那个线程（Python 做不到），但能把结论改对：
* 任务标失败、批次能收尾、失败原因里写清"建议重启后端"；
* 迟到的真实收尾不许把"超时失败"改写成"成功"——那等于骗用户说发出去了。

这里只测内存状态机，`_update_record` 被替身挡掉，不碰真实数据库。
"""

import threading
import time
import unittest
from unittest.mock import patch

import sau_backend_publish as BP


def _make_task(task_id: str, *, status: str = BP.STATUS_RUNNING) -> dict:
    """造一条结构上跟真实任务一致的内存任务（字段按 _finish_task 会读的来）。"""
    return {
        "task_id": task_id,
        "job_id": "job-test",
        "record_id": 1,
        "channel": "tencent:1",
        "platform": 2,
        "account_name": "测试账号",
        "status": status,
        "stage": "launching",
        "stage_text": "启动浏览器",
        "action": "上传前检查通过",
        "message": "",
        "started_at": time.time(),
        "finished_at": None,
        "duration": None,
        "steps": [],
        "log": [],
        "prompt": None,
    }


class WatchdogTestBase(unittest.TestCase):
    TIMEOUT = 0.2

    def setUp(self):
        self.task_id = f"wd-{time.time_ns()}"
        self._patches = [
            # 落库是替身：这一组只验证内存状态机
            patch.object(BP, "_update_record"),
            patch.object(BP, "PUBLISH_TASK_TIMEOUT", self.TIMEOUT),
        ]
        for p in self._patches:
            p.start()
        with BP._lock:
            BP._tasks[self.task_id] = _make_task(self.task_id)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        with BP._lock:
            BP._tasks.pop(self.task_id, None)

    def _task(self):
        with BP._lock:
            return dict(BP._tasks[self.task_id])

    def _wait(self, seconds=2.0):
        """等看门狗触发（轮询比 sleep 固定值稳）。"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._task()["status"] != BP.STATUS_RUNNING:
                return True
            time.sleep(0.02)
        return False


class WatchdogFiringTest(WatchdogTestBase):
    def test_stuck_task_is_marked_failed(self):
        timer = BP._arm_watchdog(self.task_id)
        self.assertTrue(self._wait(), "看门狗应当把卡住的任务判成失败")
        task = self._task()
        self.assertEqual(task["status"], BP.STATUS_FAILED)
        self.assertIn("超时", task["message"])
        BP._disarm_watchdog(timer)

    def test_message_tells_user_what_to_do(self):
        """失败原因不能只说"超时"——得说清怎么办（这类卡住重启后端才能回收资源）。"""
        BP._arm_watchdog(self.task_id)
        self.assertTrue(self._wait())
        self.assertIn("重启后端", self._task()["message"])

    def test_finished_task_is_not_touched(self):
        """任务已经正常结束，迟到的看门狗不许再改它的状态。"""
        BP._finish_task(self.task_id, BP.STATUS_SUCCESS, "已发布成功")
        BP._arm_watchdog(self.task_id)
        time.sleep(self.TIMEOUT * 3)
        task = self._task()
        self.assertEqual(task["status"], BP.STATUS_SUCCESS)
        self.assertEqual(task["message"], "已发布成功")

    def test_disarm_prevents_firing(self):
        timer = BP._arm_watchdog(self.task_id)
        BP._disarm_watchdog(timer)
        time.sleep(self.TIMEOUT * 3)
        self.assertEqual(self._task()["status"], BP.STATUS_RUNNING)

    def test_disarm_tolerates_none(self):
        BP._disarm_watchdog(None)  # 不应抛异常

    def test_unknown_task_is_ignored(self):
        BP._arm_watchdog("no-such-task")
        time.sleep(self.TIMEOUT * 3)  # 不应抛异常


class LateFinishCannotOverwriteTest(WatchdogTestBase):
    def test_late_success_does_not_rewrite_timeout_verdict(self):
        """核心回归：卡住的线程哪天真返回时，不能把"超时失败"改成"成功"。

        那是最坏的一种错——用户会以为发出去了，实际可能压根没发。
        """
        BP._arm_watchdog(self.task_id)
        self.assertTrue(self._wait())
        self.assertEqual(self._task()["status"], BP.STATUS_FAILED)

        # 模拟卡住的线程终于跑完，回调了正常收尾
        BP._finish_task(self.task_id, BP.STATUS_SUCCESS, "已发布成功")

        task = self._task()
        self.assertEqual(task["status"], BP.STATUS_FAILED, "不许被改写")
        self.assertIn("超时", task["message"])

    def test_late_failure_also_ignored(self):
        BP._arm_watchdog(self.task_id)
        self.assertTrue(self._wait())
        BP._finish_task(self.task_id, BP.STATUS_FAILED, "另一个失败原因")
        self.assertIn("超时", self._task()["message"])

    def test_normal_finish_still_works_without_watchdog(self):
        """没有看门狗插过手的任务，收尾逻辑必须照旧。"""
        BP._finish_task(self.task_id, BP.STATUS_SUCCESS, "已发布成功")
        task = self._task()
        self.assertEqual(task["status"], BP.STATUS_SUCCESS)
        self.assertIsNotNone(task["finished_at"])
        self.assertIsNotNone(task["duration"])


class WatchdogConfigTest(unittest.TestCase):
    def test_default_timeout_is_generous(self):
        """默认值不能太紧，否则正常的大视频上传会被误判成卡住。"""
        with patch.dict("os.environ", {}, clear=False):
            default = 30 * 60
        self.assertGreaterEqual(BP.PUBLISH_TASK_TIMEOUT, 60, "至少得给一分钟")
        self.assertEqual(BP.PUBLISH_TASK_TIMEOUT, default, "默认应为 30 分钟")

    def test_watchdog_is_a_daemon_thread(self):
        """看门狗必须是 daemon，否则会拖住进程退出。"""
        captured = {}

        real_timer = threading.Timer

        class RecordingTimer(real_timer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                captured["timer"] = self

        with patch.object(BP.threading, "Timer", RecordingTimer):
            timer = BP._arm_watchdog("cfg-test-task")
        self.assertTrue(captured["timer"].daemon)
        timer.cancel()


if __name__ == "__main__":
    unittest.main()
