"""看门狗能不能扛住"同一通道连续两条都卡住"。

现场复盘（2026-09-28 真实日志）：
* id=59 task=8e465053 17:07:17 启动 → 卡住 → **看门狗生效**，DB 里 status=failed；
* id=62 task=92045d5a 17:49:22 启动 → 也卡住（"该账号已有浏览器在跑，等它跑完"）
  → 但**看门狗没有生效**，记录一直挂在 running，直到第二天重启才被标成 interrupted。

所以问题不是"看门狗不工作"，而是"它只工作了第一条"。这个文件就是钉住这个场景：
同一通道上连续两条卡住的任务，都必须被判失败、通道都要能继续往下走。
"""

import queue
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import sau_backend_publish as BP


@contextmanager
def _noop_browser_session(*args, **kwargs):
    """把外层的账号锁/并发名额换成空实现——这一组测的是看门狗，不是锁。"""
    yield


def _make_task(task_id: str, channel: str) -> dict:
    return {
        "task_id": task_id,
        "job_id": "job-stuck",
        "record_id": 1,
        "channel": channel,
        "platform": 2,
        "account_id": 1,
        "account_name": "测试账号",
        "account_file": "x.json",
        "options": {},
        "schedule_at": None,
        "status": BP.STATUS_QUEUED,
        "stage": "queued",
        "stage_text": "排队中",
        "action": "已加入发布队列",
        "message": "",
        "started_at": None,
        "finished_at": None,
        "duration": None,
        "steps": [],
        "log": [],
        "prompt": None,
    }


class ConsecutiveStuckTasksTest(unittest.TestCase):
    TIMEOUT = 0.3          # 看门狗
    CHANNEL = "tencent:1"

    def setUp(self):
        self.task_ids = [f"stuck-a-{time.time_ns()}", f"stuck-b-{time.time_ns()}"]
        self._patches = [
            patch.object(BP, "PUBLISH_TASK_TIMEOUT", self.TIMEOUT),
            patch.object(BP, "_update_record"),
            patch.object(BP, "_refresh_job"),
            patch.object(BP, "browser_session", _noop_browser_session),
            patch.object(BP, "_validate_task"),
            # 上传器永远阻塞，模拟"卡住"
            patch.object(BP, "_invoke_uploader", lambda task: time.sleep(3600)),
        ]
        for p in self._patches:
            p.start()

        with BP._lock:
            for tid in self.task_ids:
                BP._tasks[tid] = _make_task(tid, self.CHANNEL)
        self.q: queue.Queue = queue.Queue()
        BP._queues[self.CHANNEL] = self.q

        # 通道线程（不走 _ensure_worker，免得它复用别的通道）
        self.worker = threading.Thread(
            target=BP._worker_loop, args=(self.CHANNEL,), daemon=True
        )
        self.worker.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        with BP._lock:
            for tid in self.task_ids:
                BP._tasks.pop(tid, None)
        BP._queues.pop(self.CHANNEL, None)

    def _status(self, tid):
        with BP._lock:
            return BP._tasks[tid]["status"]

    def _wait_status(self, tid, want, seconds=12.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._status(tid) in want:
                return True
            time.sleep(0.05)
        return False

    def test_both_stuck_tasks_get_killed(self):
        """核心：连续两条卡住，两条都要被判失败——不能只保第一条。"""
        for tid in self.task_ids:
            self.q.put(tid)

        first_ok = self._wait_status(self.task_ids[0], {BP.STATUS_FAILED})
        self.assertTrue(first_ok, f"第一条应当被看门狗判失败，实际={self._status(self.task_ids[0])}")

        second_ok = self._wait_status(self.task_ids[1], {BP.STATUS_FAILED})
        self.assertTrue(
            second_ok,
            f"第二条也必须被判失败，实际={self._status(self.task_ids[1])}"
            f"（现实里这条一直挂在 running 到第二天重启）",
        )

    def test_channel_keeps_consuming_after_first_stuck_task(self):
        """第一条卡住被放弃后，通道要能继续消费下一条（否则第二条永远停在 queued）。"""
        for tid in self.task_ids:
            self.q.put(tid)
        # 第二条至少要被"启动"过（started_at 被写上）
        deadline = time.time() + 12
        started = False
        while time.time() < deadline:
            with BP._lock:
                if BP._tasks[self.task_ids[1]].get("started_at"):
                    started = True
                    break
            time.sleep(0.05)
        self.assertTrue(started, "通道被第一条卡死了，第二条从未开始")

    def test_watchdog_verdict_is_recorded_for_each(self):
        """两条的失败原因都应当是"超时"，而不是一条超时一条别的。"""
        for tid in self.task_ids:
            self.q.put(tid)
        for tid in self.task_ids:
            self.assertTrue(self._wait_status(tid, {BP.STATUS_FAILED}), f"{tid} 未被判失败")
        for tid in self.task_ids:
            with BP._lock:
                msg = BP._tasks[tid]["message"]
            self.assertIn("超时", msg, f"{tid} 的失败原因不对: {msg!r}")


if __name__ == "__main__":
    unittest.main()
