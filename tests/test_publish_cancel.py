"""手动中断发布任务（sau_backend_publish.py + myUtils/browser_lock.py）。

由来（用户诉求）："发布中心每一个发布中的任务支持手动中断，不然有些任务太慢了一直
卡着"。在此之前只有批次级的「取消未开始的任务」，正在跑的那条**没有任何办法停下来**，
而看门狗要等满 30 分钟——这中间用户只能干看着。

中断为什么是两条腿
------------------
Python 杀不掉线程，所以中断拆成两层：

1. **协作式**：任务线程拿一个 `threading.Event`，在它自己控制的等待点上回头看一眼
   （等账号锁 / 等并发名额），被中断就抛 `PublishCancelled` 干净退出；
2. **异步式**：执行器自己建事件循环并把 `(loop, Task)` 登记出来，HTTP 线程用
   `loop.call_soon_threadsafe(task.cancel)` 把 `CancelledError` 抛进正在 await 的协程。

再加一条**不能省**的规矩：中断接口**先给结论、再放通道**，绝不等线程自己收尾——
它可能永远不返回（等一个不再响应的驱动是老问题），等它就等于没中断。
这里测的就是这三件事，外加"迟到的成功不许改写已取消"这条最要命的回归。
"""

import asyncio
import queue
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask

import myUtils.browser_lock as BL
import sau_backend_publish as BP


@contextmanager
def _noop_browser_session(*args, **kwargs):
    """把账号锁 / 并发名额换成空实现——通道那一组测的是中断，不是锁。"""
    yield


def _make_task(task_id: str, *, status: str = BP.STATUS_RUNNING, job_id: str = "job-test") -> dict:
    """造一条结构上跟真实任务一致的内存任务（字段按 `_finish_task` 会读的来）。"""
    return {
        "task_id": task_id,
        "job_id": job_id,
        "record_id": 1,
        "channel": "douyin:1",
        "platform": 3,
        "platform_name": "抖音",
        "account_id": 1,
        "account_name": "测试账号",
        "material": "demo.mp4",
        "material_title": "演示素材",
        "title": "演示标题",
        "status": status,
        "stage": "uploading",
        "stage_text": "上传视频",
        "action": "小人正在努力上传视频",
        "message": "",
        "started_at": time.time(),
        "finished_at": None,
        "duration": None,
        "steps": [],
        "log": [],
        "prompt": None,
        "_cancel_event": threading.Event(),
    }


class CancelTestBase(unittest.TestCase):
    def setUp(self):
        self.task_id = f"cx-{time.time_ns()}"
        self._patches = [patch.object(BP, "_update_record")]
        for p in self._patches:
            p.start()
        with BP._lock:
            BP._tasks[self.task_id] = _make_task(self.task_id)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        with BP._lock:
            BP._tasks.pop(self.task_id, None)

    def _task(self) -> dict:
        with BP._lock:
            return dict(BP._tasks[self.task_id])


class CancelRunningTaskTest(CancelTestBase):
    def test_running_task_is_marked_cancelled(self):
        ok, message, previous = BP._cancel_task(self.task_id)
        self.assertTrue(ok)
        self.assertEqual(previous, BP.STATUS_RUNNING)
        task = self._task()
        self.assertEqual(task["status"], BP.STATUS_CANCELLED)
        self.assertIsNotNone(task["finished_at"], "必须当场给结论，不能等线程收尾")

    def test_message_tells_user_to_verify_on_the_platform(self):
        """中断一个发到一半的任务是有代价的，说明里必须让用户去平台确认。"""
        BP._cancel_task(self.task_id)
        self.assertIn("平台确认", self._task()["message"])

    def test_interrupt_sets_the_cooperative_flag(self):
        BP._cancel_task(self.task_id)
        self.assertTrue(self._task()["_cancel_event"].is_set())

    def test_channel_is_released_immediately(self):
        """核心：通道要立刻放行去跑下一个任务，不能干等这个可能永远不返回的线程。"""
        concluded = threading.Event()
        with BP._lock:
            BP._tasks[self.task_id]["_concluded"] = concluded
        BP._cancel_task(self.task_id)
        self.assertTrue(concluded.is_set(), "通道必须立刻脱身")

    def test_late_success_cannot_rewrite_the_cancellation(self):
        """最要命的回归：用户点了中断，那个线程后来才返回，不能改写成「成功」。

        那等于骗用户说"发出去了"——而它很可能只发了一半，甚至压根没发。
        """
        BP._cancel_task(self.task_id)
        # 模拟卡住的线程终于跑完，回调了正常收尾
        BP._finish_task(self.task_id, BP.STATUS_SUCCESS, "已发布成功")
        task = self._task()
        self.assertEqual(task["status"], BP.STATUS_CANCELLED, "不许被改写")
        self.assertNotEqual(task["message"], "已发布成功")

    def test_late_failure_also_ignored(self):
        BP._cancel_task(self.task_id)
        BP._finish_task(self.task_id, BP.STATUS_FAILED, "另一个失败原因")
        self.assertEqual(self._task()["status"], BP.STATUS_CANCELLED)


class CancelQueuedTaskTest(CancelTestBase):
    def setUp(self):
        super().setUp()
        with BP._lock:
            BP._tasks[self.task_id]["status"] = BP.STATUS_QUEUED
            BP._waiting["douyin:1"] = [self.task_id]

    def tearDown(self):
        with BP._lock:
            BP._waiting.pop("douyin:1", None)
        super().tearDown()

    def test_queued_task_is_marked_cancelled(self):
        ok, message, previous = BP._cancel_task(self.task_id)
        self.assertTrue(ok)
        self.assertEqual(previous, BP.STATUS_QUEUED)
        self.assertEqual(self._task()["status"], BP.STATUS_CANCELLED)

    def test_queued_cancel_does_not_freeze_the_verdict(self):
        """排队中的任务线程还没跑，不需要「结论定死」那套——它本来就没什么可改写的。"""
        BP._cancel_task(self.task_id)
        self.assertFalse(self._task().get("_stuck"))

    def test_removed_from_channel_waiting_list(self):
        BP._cancel_task(self.task_id)
        with BP._lock:
            self.assertNotIn(self.task_id, BP._waiting.get("douyin:1", []))

    def test_cancelled_queued_task_never_opens_a_browser(self):
        """排队时被取消的任务，worker 轮到它时必须直接跳过，别白开一个浏览器。"""
        BP._cancel_task(self.task_id)
        uploader = Mock()
        with patch.object(BP, "_invoke_uploader", uploader):
            BP._run_task(self.task_id)
        uploader.assert_not_called()
        self.assertEqual(self._task()["status"], BP.STATUS_CANCELLED)


class CancelRefusalTest(CancelTestBase):
    def test_missing_task_is_refused(self):
        ok, message, previous = BP._cancel_task("no-such-task")
        self.assertFalse(ok)
        self.assertIn("不存在", message)

    def test_finished_task_is_refused(self):
        BP._finish_task(self.task_id, BP.STATUS_SUCCESS, "已发布成功")
        ok, message, _previous = BP._cancel_task(self.task_id)
        self.assertFalse(ok)
        self.assertIn("成功", message)
        self.assertEqual(self._task()["status"], BP.STATUS_SUCCESS)

    def test_already_cancelled_is_idempotent_success(self):
        """用户连点两下中断不该报错——第二下点的时候它已经是「已取消」了。"""
        BP._cancel_task(self.task_id)
        ok, _message, previous = BP._cancel_task(self.task_id)
        self.assertTrue(ok)
        self.assertEqual(previous, BP.STATUS_CANCELLED)


class _HoldLock:
    """在**另一个线程**里占着锁不放。

    和 `test_browser_lock_timeout.py` 里同样的理由：`RLock` 可重入，在同一个线程里
    先 acquire 再调 browser_session 根本测不出竞争——现实里的竞争本来就来自别的线程。
    """

    def __init__(self, lock):
        self.lock = lock
        self.held = threading.Event()
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        self.lock.acquire()
        self.held.set()
        self.done.wait(10)
        self.lock.release()

    def __enter__(self):
        self.thread.start()
        if not self.held.wait(2):
            raise AssertionError("占锁线程没能拿到锁")
        return self

    def __exit__(self, *exc):
        self.done.set()
        self.thread.join(3)
        return False


class CancelDuringLockWaitTest(unittest.TestCase):
    """排队期间的中断：这是最常见、也是收尾最干净的取消（浏览器一次都没开）。"""

    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "cookie.json"

    def test_cancel_interrupts_the_account_lock_wait(self):
        cancel = threading.Event()
        with _HoldLock(BL.account_lock(self.path)):
            # 不点中断的话要干等 LOCK_WAIT_TIMEOUT（默认 10 分钟）
            with patch.object(BL, "LOCK_WAIT_TIMEOUT", 60):
                threading.Timer(0.2, cancel.set).start()
                started = time.time()
                with self.assertRaises(BL.BrowserSessionCancelled):
                    with BL.browser_session(self.path, cancel_event=cancel):
                        self.fail("被中断了就不该进到 with 里")
                elapsed = time.time() - started
        self.assertLess(elapsed, 5, "中断必须立刻生效，不能干等满等锁上限")

    def test_cancel_releases_the_account_lock(self):
        """被中断之后账号锁必须放掉，否则下一个用这个账号的任务又卡住了。"""
        cancel = threading.Event()
        with _HoldLock(BL.account_lock(self.path)):
            with patch.object(BL, "LOCK_WAIT_TIMEOUT", 60):
                threading.Timer(0.2, cancel.set).start()
                with self.assertRaises(BL.BrowserSessionCancelled):
                    with BL.browser_session(self.path, cancel_event=cancel):
                        pass
        # 锁已经放掉：现在能立刻拿到
        lock = BL.account_lock(self.path)
        self.assertTrue(lock.acquire(blocking=False), "账号锁泄漏了")
        lock.release()

    def test_cancel_while_waiting_for_a_slot_releases_the_account_lock(self):
        """卡在"等并发名额"上被中断：账号锁同样不能泄漏。"""
        cancel = threading.Event()
        taken = BL.MAX_CONCURRENT_BROWSERS
        for _ in range(taken):
            BL.BROWSER_SLOTS.acquire()
        try:
            result: dict = {}

            def _worker():
                try:
                    with BL.browser_session(self.path, cancel_event=cancel):
                        result["entered"] = True
                except BL.BrowserSessionCancelled:
                    result["cancelled"] = True
                except BaseException as exc:  # noqa: BLE001 - 测试要看到所有意外
                    result["error"] = repr(exc)

            thread = threading.Thread(target=_worker, daemon=True)
            thread.start()
            time.sleep(0.2)
            cancel.set()
            thread.join(5)
        finally:
            for _ in range(taken):
                BL.BROWSER_SLOTS.release()

        self.assertTrue(result.get("cancelled"), f"应被中断，实际: {result}")
        self.assertNotIn("entered", result)
        lock = BL.account_lock(self.path)
        self.assertTrue(lock.acquire(blocking=False), "账号锁泄漏了")
        lock.release()

    def test_uncancelled_session_still_works_normally(self):
        """没有中断时行为必须和以前完全一样。"""
        with BL.browser_session(self.path, cancel_event=threading.Event()) as _:
            lock = BL.account_lock(self.path)
            self.assertTrue(lock.acquire(blocking=False))
            lock.release()


class AsyncInterruptTest(unittest.TestCase):
    """异步那条腿：把 `CancelledError` 抛进正在 await 的协程里。"""

    def setUp(self):
        self.task_id = f"async-{time.time_ns()}"

    def tearDown(self):
        BP._unregister_async_run(self.task_id)

    def _wait_registered(self, seconds=3.0) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            with BP._async_runs_lock:
                if self.task_id in BP._async_runs:
                    return True
            time.sleep(0.02)
        return False

    def test_running_coroutine_is_actually_interrupted(self):
        """核心：一个 await 住不放的协程，能被 HTTP 线程打断并干净退出。"""
        started = threading.Event()
        outcome: dict = {}

        async def forever():
            started.set()
            await asyncio.sleep(30)

        def _runner():
            try:
                BP._run_uploader_async(forever(), self.task_id, threading.Event())
                outcome["result"] = "returned"
            except BP.PublishCancelled:
                outcome["result"] = "cancelled"
            except BaseException as exc:  # noqa: BLE001 - 测试要看到所有意外
                outcome["result"] = f"error: {exc!r}"

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        self.assertTrue(started.wait(3), "协程没跑起来")
        self.assertTrue(self._wait_registered(), "没登记到 _async_runs，中断就无从下手")
        self.assertTrue(BP._interrupt_async_run(self.task_id))
        thread.join(5)
        self.assertFalse(thread.is_alive(), "协程应当被中断后立刻退出")
        self.assertEqual(outcome.get("result"), "cancelled")

    def test_cancelled_error_becomes_publish_cancelled(self):
        """`CancelledError` 是 BaseException，不翻译的话 `except Exception` 接不住，
        任务会跳过收尾跑掉——批次就永远不 done 了。"""
        outcome: dict = {}

        async def forever():
            await asyncio.sleep(30)

        def _runner():
            try:
                BP._run_uploader_async(forever(), self.task_id, threading.Event())
            except BP.PublishCancelled as exc:
                outcome["ok"] = isinstance(exc, Exception)
            except BaseException as exc:  # noqa: BLE001
                outcome["bad"] = repr(exc)

        # 自己在登记后立刻取消
        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        self.assertTrue(self._wait_registered())
        BP._interrupt_async_run(self.task_id)
        thread.join(5)
        self.assertTrue(outcome.get("ok"), f"应当是 PublishCancelled，实际: {outcome}")

    def test_normal_coroutine_completes_and_cleans_up(self):
        async def quick():
            await asyncio.sleep(0)
            return "done"

        BP._run_uploader_async(quick(), self.task_id, threading.Event())
        with BP._async_runs_lock:
            self.assertNotIn(self.task_id, BP._async_runs, "跑完必须撤销登记")

    def test_interrupt_unknown_task_is_harmless(self):
        self.assertFalse(BP._interrupt_async_run("no-such-async-task"))

    def test_registration_is_removed_even_when_coroutine_raises(self):
        async def boom():
            raise RuntimeError("上传器炸了")

        with self.assertRaises(RuntimeError):
            BP._run_uploader_async(boom(), self.task_id, threading.Event())
        with BP._async_runs_lock:
            self.assertNotIn(self.task_id, BP._async_runs)


class CancelEndpointTest(unittest.TestCase):
    def setUp(self):
        self.task_id = f"ep-{time.time_ns()}"
        self.job_id = f"job-{time.time_ns()}"
        self._patches = [patch.object(BP, "_update_record")]
        for p in self._patches:
            p.start()
        with BP._lock:
            BP._tasks[self.task_id] = _make_task(self.task_id, job_id=self.job_id)
            BP._jobs[self.job_id] = {
                "job_id": self.job_id,
                "created_at": time.time(),
                "status": "running",
                "task_ids": [self.task_id],
                "options": {},
            }
        app = Flask(__name__)
        app.register_blueprint(BP.publish_bp)
        self.client = app.test_client()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        with BP._lock:
            BP._tasks.pop(self.task_id, None)
            BP._jobs.pop(self.job_id, None)

    def test_cancel_endpoint_interrupts_a_running_task(self):
        res = self.client.post(f"/publish/task/{self.task_id}/cancel")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["code"], 200)
        with BP._lock:
            self.assertEqual(BP._tasks[self.task_id]["status"], BP.STATUS_CANCELLED)

    def test_cancel_endpoint_404s_for_unknown_task(self):
        res = self.client.post("/publish/task/does-not-exist/cancel")
        self.assertEqual(res.status_code, 404)

    def test_cancel_endpoint_400s_for_a_finished_task(self):
        BP._finish_task(self.task_id, BP.STATUS_SUCCESS, "已发布成功")
        res = self.client.post(f"/publish/task/{self.task_id}/cancel")
        self.assertEqual(res.status_code, 400)

    def test_job_cancel_still_only_touches_queued_by_default(self):
        """默认行为不能变：老界面上的「取消未开始的任务」不该顺手掐掉正在跑的。"""
        res = self.client.post(f"/publish/job/{self.job_id}/cancel")
        self.assertEqual(res.status_code, 200)
        with BP._lock:
            self.assertEqual(BP._tasks[self.task_id]["status"], BP.STATUS_RUNNING)

    def test_job_cancel_with_running_flag_interrupts_running_tasks(self):
        res = self.client.post(f"/publish/job/{self.job_id}/cancel", json={"running": True})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["data"]["interrupted"], 1)
        with BP._lock:
            self.assertEqual(BP._tasks[self.task_id]["status"], BP.STATUS_CANCELLED)

    def test_job_cancel_with_query_flag_also_works(self):
        res = self.client.post(f"/publish/job/{self.job_id}/cancel?running=1")
        self.assertEqual(res.status_code, 200)
        with BP._lock:
            self.assertEqual(BP._tasks[self.task_id]["status"], BP.STATUS_CANCELLED)


class PublicTaskShapeTest(CancelTestBase):
    def test_cancellable_flag_is_exposed(self):
        task = self._task()
        public = BP._public_task(task, {})
        self.assertTrue(public["cancellable"])

    def test_finished_task_is_not_cancellable(self):
        BP._finish_task(self.task_id, BP.STATUS_SUCCESS, "已发布成功")
        public = BP._public_task(self._task(), {})
        self.assertFalse(public["cancellable"])

    def test_internal_cancel_event_never_reaches_the_frontend(self):
        """`threading.Event` 不能漏进 JSON——json_safe 会把它转成 "<threading.Event …>"。"""
        public = BP._public_task(self._task(), {})
        self.assertNotIn("_cancel_event", public)
        self.assertNotIn("cancel_event", public)
        self.assertNotIn("_concluded", public)


class ChannelUnblocksOnCancelTest(unittest.TestCase):
    """用户真正在意的那件事：中断一条卡住的任务之后，**同一账号后面的任务要能跑起来**。

    在此之前只有看门狗能脱身，而它要等满 `PUBLISH_TASK_TIMEOUT`（默认 30 分钟）——
    这半小时里同一个账号后面排的任务全在干等，用户看到的就是"一直卡着"。
    手动中断必须立刻放通道走。
    """

    CHANNEL = "douyin:9"

    def setUp(self):
        stamp = time.time_ns()
        self.hang_id = f"hang-{stamp}"
        self.next_id = f"next-{stamp}"
        self._patches = [
            # 看门狗设得足够长：这里要证明的是**中断**能脱身，不是超时能脱身
            patch.object(BP, "PUBLISH_TASK_TIMEOUT", 3600),
            patch.object(BP, "_update_record"),
            patch.object(BP, "_refresh_job"),
            patch.object(BP, "_validate_task"),
            patch.object(BP, "browser_session", _noop_browser_session),
        ]
        for p in self._patches:
            p.start()

        hang_id = self.hang_id

        def _fake_uploader(task):
            if task["task_id"] == hang_id:
                time.sleep(3600)      # 卡住：像等一个不再响应的驱动
            return None

        self._uploader_patch = patch.object(BP, "_invoke_uploader", _fake_uploader)
        self._uploader_patch.start()

        with BP._lock:
            for tid in (self.hang_id, self.next_id):
                task = _make_task(tid, status=BP.STATUS_QUEUED)
                task.update(
                    channel=self.CHANNEL,
                    account_file="x.json",
                    options={},
                    schedule_at=None,
                    started_at=None,
                )
                BP._tasks[tid] = task
        self.q: "queue.Queue[str]" = queue.Queue()
        BP._queues[self.CHANNEL] = self.q
        self.worker = threading.Thread(
            target=BP._worker_loop, args=(self.CHANNEL,), daemon=True
        )
        self.worker.start()

    def tearDown(self):
        self._uploader_patch.stop()
        for p in self._patches:
            p.stop()
        with BP._lock:
            for tid in (self.hang_id, self.next_id):
                BP._tasks.pop(tid, None)
        BP._queues.pop(self.CHANNEL, None)

    def _wait_for(self, predicate, seconds=10.0) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def _status(self, tid) -> str:
        with BP._lock:
            return BP._tasks[tid]["status"]

    def test_cancelling_a_hung_task_lets_the_next_one_start(self):
        self.q.put(self.hang_id)
        self.q.put(self.next_id)

        self.assertTrue(
            self._wait_for(lambda: self._status(self.hang_id) == BP.STATUS_RUNNING),
            "第一条应当先跑起来",
        )

        BP._cancel_task(self.hang_id)
        self.assertEqual(self._status(self.hang_id), BP.STATUS_CANCELLED,
                         "中断必须当场给结论（线程还卡着，不能等它）")

        self.assertTrue(
            self._wait_for(lambda: self._status(self.next_id) == BP.STATUS_SUCCESS),
            f"卡住的那条被中断后，下一条必须立刻跑起来，实际={self._status(self.next_id)}"
            f"（中断前它要等满 30 分钟看门狗才能轮到）",
        )

    def test_hung_task_is_never_rewritten_to_success(self):
        """中断之后，那条卡住的线程哪天真返回了，也不许把结论改成「成功」。"""
        self.q.put(self.hang_id)
        self.assertTrue(
            self._wait_for(lambda: self._status(self.hang_id) == BP.STATUS_RUNNING)
        )
        BP._cancel_task(self.hang_id)

        # 模拟它终于返回并回调正常收尾
        BP._finish_task(self.hang_id, BP.STATUS_SUCCESS, "已发布成功")
        with BP._lock:
            status = BP._tasks[self.hang_id]["status"]
            message = BP._tasks[self.hang_id]["message"]
        self.assertEqual(status, BP.STATUS_CANCELLED, "不许被改写")
        self.assertNotEqual(message, "已发布成功")


if __name__ == "__main__":
    unittest.main()
