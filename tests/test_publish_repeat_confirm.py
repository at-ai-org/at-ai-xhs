"""发布中心「同账号已发过的素材再发一次要二次确认」（sau_backend_publish.py + publish_records.py）。

由来（用户诉求）："发布中心同账号已发布的素材再次发布时要二次确认"。
场景很实在——素材发完还留在发布列表里，再点一次「开始发布」就会在同一个账号下
多出一条重复内容，而界面上当时没有任何提示。

这里的取舍是**只提醒、不拦截**：
* 后端 `/publish/check-published` 只查发布记录、把"哪条素材在这个账号发过"查出来；
* 前端据此弹窗让人确认，确认结果跟着 `options.confirmRepublish` 回传；
* `/publish/submit` **不**因为重复就报错——重发是合法操作（上次效果不好想再发一次），
  挡在服务端只会让"确认过了还要再确认"变成死循环。提交时给这些任务补一行日志，
  事后翻发布记录能分清"知情重发"和"压根没人提过"。

要钉住的几条：
1. 失败/取消/中断的记录**不算**"已发布"——那种情况下重发是正常的，不该打扰人；
2. 素材改名（生成 AI 文案时会被改名成标题）前后都要认得出来；
3. 小红书工作台写的记录没有 account_id，只按 id 查会漏，得认账号名；
4. 提交接口对重复发布保持放行（这条是**故意**的行为，别被后来人"顺手"改成 4xx）。
"""

import queue
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import myUtils.publish_records as records
import sau_backend_publish as BP


class RepeatTestBase(unittest.TestCase):
    """每个用例一个临时库：绝不碰 `db/database.db` 里用户真实的发布记录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "database.db"
        self._patch = patch.object(records, "DB_PATH", self.db_path)
        self._patch.start()
        records.ensure_tables()
        with records.db() as conn:
            # 发布中心要读的两张上游表（账号 / 素材展示名）
            conn.execute(
                "CREATE TABLE user_info (id INTEGER PRIMARY KEY, userName TEXT, type INTEGER, filePath TEXT)"
            )
            conn.execute(
                "CREATE TABLE file_records (id INTEGER PRIMARY KEY, file_path TEXT, filename TEXT)"
            )
            conn.execute("INSERT INTO user_info (id, userName, type, filePath) VALUES (1, '账号A', 1, 'a.json')")
            conn.execute("INSERT INTO user_info (id, userName, type, filePath) VALUES (2, '账号B', 1, 'b.json')")

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    # ---- 造记录 ---- #
    def record(
        self,
        material,
        *,
        material_title=None,
        platform=1,
        account_id=1,
        account_name="账号A",
        status=records.STATUS_SUCCESS,
        created_at="2026-09-28 12:00:00",
        title="上次的标题",
        source=records.SOURCE_PUBLISH_CENTER,
    ):
        with records.db() as conn:
            cur = conn.execute(
                """
                INSERT INTO publish_records
                    (job_id, task_id, source, platform, platform_name, account_id, account_name,
                     material, material_title, title, status, created_at)
                VALUES ('job-1', 'task-1', ?, ?, '小红书', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    platform,
                    account_id,
                    account_name,
                    material,
                    material_title if material_title is not None else material,
                    title,
                    status,
                    created_at,
                ),
            )
            return int(cur.lastrowid)


class FindRepeatPublishesTest(RepeatTestBase):
    """`find_repeat_publishes` 的匹配口径。"""

    TARGET = [{"platform": 1, "account_id": 1, "account_name": "账号A"}]

    def test_success_record_counts_as_published(self):
        self.record("demo.mp4")

        found = records.find_repeat_publishes(self.TARGET, ["demo.mp4"])

        self.assertEqual(len(found), 1)
        item = found[0]
        self.assertTrue(item["published"])
        self.assertFalse(item["in_flight"])
        self.assertEqual(item["platform_name"], "小红书")
        self.assertEqual(item["account_name"], "账号A")
        self.assertEqual(item["last_time"], "2026-09-28 12:00:00")
        self.assertEqual(item["last_status_text"], "成功")
        self.assertEqual(item["last_source_text"], "发布中心")
        self.assertEqual(item["last_title"], "上次的标题")
        self.assertIn("成功发布过", item["message"])

    def test_nothing_published_returns_empty(self):
        self.assertEqual(records.find_repeat_publishes(self.TARGET, ["demo.mp4"]), [])

    def test_unfinished_statuses_do_not_count_as_published(self):
        """失败/取消/中断都不算"已发布"——那种情况下重发是正常的，不该弹窗打扰人。"""
        for status in (
            records.STATUS_FAILED,
            records.STATUS_CANCELLED,
            records.STATUS_INTERRUPTED,
        ):
            self.record("demo.mp4", status=status)

        self.assertEqual(records.find_repeat_publishes(self.TARGET, ["demo.mp4"]), [])

    def test_in_flight_record_is_reported_as_such(self):
        """还在排队/发布中的也要拦一下：再提交一次同样是重复。"""
        self.record("demo.mp4", status=records.STATUS_QUEUED)

        item = records.find_repeat_publishes(self.TARGET, ["demo.mp4"])[0]

        self.assertFalse(item["published"])
        self.assertTrue(item["in_flight"])
        self.assertEqual(item["last_status_text"], "排队中")
        self.assertIn("排队中", item["message"])

    def test_published_and_in_flight_together(self):
        self.record("demo.mp4", created_at="2026-09-27 09:00:00")
        self.record("demo.mp4", status=records.STATUS_RUNNING, created_at="2026-09-28 12:00:00")

        item = records.find_repeat_publishes(self.TARGET, ["demo.mp4"])[0]

        self.assertTrue(item["published"])
        self.assertTrue(item["in_flight"])
        self.assertEqual(item["count"], 2)
        self.assertEqual(item["published_count"], 1)
        self.assertEqual(item["in_flight_count"], 1)
        # 最近一条（id 大的）是正在跑的那条，但"发过"的时间点取成功那条
        self.assertEqual(item["last_time"], "2026-09-28 12:00:00")
        self.assertEqual(item["last_success_time"], "2026-09-27 09:00:00")
        self.assertIn("成功发布过", item["message"])
        self.assertIn("发布中", item["message"])

    def test_matches_after_material_was_renamed(self):
        """素材生成 AI 文案时会被改名：记录里存的是发布当时的名字，两边都要认。

        这正是最容易重复发的场景：改名前的记录 + 改名后再选中同一条素材。
        """
        # 记录是改名**前**发的：material 是旧文件名，material_title 已经是新名字
        self.record("旧名字.mp4", material_title="新名字（标题）")

        by_new = records.find_repeat_publishes(self.TARGET, ["新名字（标题）"])
        by_old = records.find_repeat_publishes(self.TARGET, ["旧名字.mp4"])

        self.assertEqual([i["material"] for i in by_new], ["新名字（标题）"])
        self.assertEqual([i["material"] for i in by_old], ["旧名字.mp4"])

    def test_matches_workbench_record_that_has_no_account_id(self):
        """小红书工作台的记录刻意留空 account_id（跟发布中心不是同一套账号）。"""
        self.record(
            "demo.mp4",
            account_id=0,
            account_name="账号A",
            source=records.SOURCE_XHS_WORKBENCH,
        )

        item = records.find_repeat_publishes(self.TARGET, ["demo.mp4"])[0]

        self.assertTrue(item["published"])
        self.assertEqual(item["last_source_text"], "小红书工作台")

    def test_other_platform_or_account_does_not_count(self):
        self.record("demo.mp4", platform=3, account_id=1, account_name="账号A")   # 抖音
        self.record("demo.mp4", account_id=2, account_name="账号B")               # 另一个账号

        self.assertEqual(records.find_repeat_publishes(self.TARGET, ["demo.mp4"]), [])

    def test_other_material_does_not_count(self):
        self.record("别的素材.mp4")
        self.assertEqual(records.find_repeat_publishes(self.TARGET, ["demo.mp4"]), [])

    def test_one_query_per_target_even_with_many_materials(self):
        """一个账号只查一次库（一批 50 条素材不该打 50 次查询）。"""
        self.record("a.mp4")
        queries = []
        real_db = records.db

        def counting_db():
            queries.append(1)
            return real_db()

        with patch.object(records, "db", counting_db):
            found = records.find_repeat_publishes(self.TARGET, ["a.mp4", "b.mp4", "c.mp4"])

        self.assertEqual(len(found), 1)
        self.assertEqual(len(queries), 1)

    def test_skips_targets_without_platform_or_account(self):
        self.record("demo.mp4")
        targets = [
            {"platform": 0, "account_id": 1, "account_name": "账号A"},
            {"platform": 1, "account_id": 0, "account_name": ""},
            {"platform": "x", "account_id": 1, "account_name": "账号A"},
        ]
        self.assertEqual(records.find_repeat_publishes(targets, ["demo.mp4"]), [])

    def test_empty_inputs_do_not_touch_the_database(self):
        self.assertEqual(records.find_repeat_publishes(self.TARGET, []), [])
        self.assertEqual(records.find_repeat_publishes([], ["demo.mp4"]), [])


class CheckPublishedEndpointTest(RepeatTestBase):
    """`POST /publish/check-published`：前端二次确认的数据源。"""

    def setUp(self):
        super().setUp()
        app = Flask(__name__)
        app.register_blueprint(BP.publish_bp)
        self.client = app.test_client()

    def body(self, materials=("demo.mp4",), account_ids=(1,)):
        return {
            "items": [{"material": name, "title": "标题"} for name in materials],
            "targets": [{"platform": 1, "account_ids": list(account_ids)}],
        }

    def post(self, payload):
        res = self.client.post("/publish/check-published", json=payload)
        self.assertEqual(res.status_code, 200)
        return res.get_json()["data"]

    def test_reports_a_repeat(self):
        self.record("demo.mp4")

        data = self.post(self.body())

        self.assertEqual(data["total"], 1)
        self.assertEqual(data["published"], 1)
        self.assertEqual(data["task_count"], 1)
        conflict = data["conflicts"][0]
        self.assertEqual(conflict["platform_name"], "小红书")
        self.assertEqual(conflict["account_name"], "账号A")
        self.assertEqual(conflict["material"], "demo.mp4")
        self.assertIn("成功发布过", conflict["message"])

    def test_clean_batch_reports_nothing(self):
        data = self.post(self.body())
        self.assertEqual(data["conflicts"], [])
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["task_count"], 1)

    def test_counts_tasks_across_accounts_and_materials(self):
        data = self.post(self.body(materials=("a.mp4", "b.mp4"), account_ids=(1, 2)))
        self.assertEqual(data["task_count"], 4)

    def test_broken_params_do_not_raise(self):
        """脏参数只跳过那些组合：这个接口是提醒人的，不该把发布本身挡下来。"""
        data = self.post(
            {
                "items": [{"material": ""}, "字符串", {"title": "没有素材名"}],
                "targets": [
                    {"platform": 99, "account_ids": [1]},
                    {"platform": 1, "account_ids": ["abc"]},
                    "字符串",
                ],
            }
        )
        self.assertEqual(data["conflicts"], [])
        self.assertEqual(data["task_count"], 0)

    def test_deleted_account_still_matched_by_id(self):
        """账号被删了、user_info 里查不到名字，也还能按 id 认出记录。

        这种情况下账号名取记录里那个名字：目标账号已经没了，"记录里当时叫什么"
        比"账号9"更能让人对上号。
        """
        self.record("demo.mp4", account_id=9, account_name="已删除的账号")

        data = self.post(self.body(account_ids=(9,)))

        self.assertEqual(data["total"], 1)
        self.assertEqual(data["conflicts"][0]["account_name"], "已删除的账号")

    def test_repeat_from_another_platform_is_not_reported(self):
        self.record("demo.mp4", platform=3, account_name="账号A")
        self.assertEqual(self.post(self.body())["total"], 0)


class SubmitRepeatNoteTest(RepeatTestBase):
    """提交时把"重复发布"记进任务日志（拦不拦是前端的事，留痕是后端的事）。"""

    def setUp(self):
        super().setUp()
        app = Flask(__name__)
        app.register_blueprint(BP.publish_bp)
        self.client = app.test_client()

        # 不让 worker 真的开线程跑发布：这里测的是提交那一刻干了什么。
        # `_queues` 换成 defaultdict，否则被替身的 `_ensure_worker` 不会建队列，入队会 KeyError。
        self._saved = {
            "tasks": dict(BP._tasks),
            "jobs": dict(BP._jobs),
            "waiting": {k: list(v) for k, v in BP._waiting.items()},
            "queues": dict(BP._queues),
        }
        BP._tasks.clear()
        BP._jobs.clear()
        BP._waiting.clear()
        BP._queues = __import__("collections").defaultdict(queue.Queue)
        self._patches = [patch.object(BP, "_ensure_worker", lambda channel: None)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        BP._tasks.clear()
        BP._tasks.update(self._saved["tasks"])
        BP._jobs.clear()
        BP._jobs.update(self._saved["jobs"])
        BP._waiting.clear()
        BP._waiting.update(self._saved["waiting"])
        BP._queues = self._saved["queues"]
        super().tearDown()

    def submit(self, *, confirm=None):
        options = {} if confirm is None else {"confirmRepublish": confirm}
        res = self.client.post(
            "/publish/submit",
            json={
                "items": [{"material": "demo.mp4", "title": "这次的标题"}],
                "targets": [{"platform": 1, "account_ids": [1]}],
                "options": options,
            },
        )
        self.assertEqual(res.status_code, 200)
        return res.get_json()["data"]

    def logs(self, job_id):
        with BP._lock:
            task_ids = BP._jobs[job_id]["task_ids"]
            return [line["text"] for line in BP._tasks[task_ids[0]]["log"]]

    def test_repeat_without_confirmation_is_logged(self):
        self.record("demo.mp4")

        data = self.submit()

        logs = self.logs(data["job_id"])
        self.assertEqual(len(logs), 1)
        self.assertIn("重复发布", logs[0])
        self.assertIn("没有经过二次确认", logs[0])

    def test_repeat_with_confirmation_says_so(self):
        self.record("demo.mp4")

        logs = self.logs(self.submit(confirm=True)["job_id"])

        # 这行日志是给人看的，整句话读得通才算数
        self.assertEqual(
            logs[0],
            "重复发布：这个账号已于 2026-09-28 12:00:00 成功发布过，用户已确认重发",
        )

    def test_first_publish_has_no_note(self):
        self.assertEqual(self.logs(self.submit()["job_id"]), [])

    def test_failed_attempt_is_not_a_repeat(self):
        """上次没发成功的不算重复——日志里不该冒出一条莫名其妙的"重复发布"。"""
        self.record("demo.mp4", status=records.STATUS_FAILED)
        self.assertEqual(self.logs(self.submit()["job_id"]), [])

    def test_submit_still_goes_through_without_confirmation(self):
        """**故意不拦**：重发是合法操作（上次效果不好想再发一次）。

        挡在服务端会让"确认过了还要再确认"变成死循环，所以这里钉死：未经确认的重复
        提交照样 200，只是日志里留一句。
        """
        self.record("demo.mp4")

        data = self.submit()

        self.assertTrue(data["job_id"])
        self.assertEqual(data["task_count"], 1)

    def test_only_the_repeating_task_gets_the_note(self):
        """同批里没发过的那条不该被扣上"重复发布"。"""
        self.record("demo.mp4")
        res = self.client.post(
            "/publish/submit",
            json={
                "items": [
                    {"material": "demo.mp4", "title": "发过的"},
                    {"material": "新的.mp4", "title": "没发过的"},
                ],
                "targets": [{"platform": 1, "account_ids": [1]}],
                "options": {},
            },
        )
        data = res.get_json()["data"]
        with BP._lock:
            task_ids = BP._jobs[data["job_id"]]["task_ids"]
            notes = {BP._tasks[tid]["material"]: BP._tasks[tid]["log"] for tid in task_ids}

        self.assertIn("重复发布", notes["demo.mp4"][0]["text"])
        self.assertEqual(notes["新的.mp4"], [])


class SkipPublishedTest(RepeatTestBase):
    """「跳过已发布素材」：发布中心那个默认勾着的选项（`skip_published`）。

    和上面那个二次确认是**两件事**，别混：

    * 二次确认问的是"**这个账号**发过没有"，只提示、不拦；
    * 这个选项问的是"**这个平台**上成功发过没有"，答"发过"就**根本不建这条任务**。

    要钉住的几条：
    1. 按**平台**判：同一条素材发给同一个平台的多个账号，只要有一个账号发成功过，
       这个平台整个跳过（不是只跳那一个账号）；
    2. 别的平台不受影响——素材在快手发过，不妨碍它发抖音；
    3. 只有 `success` 算数：失败/取消/中断过的重发是正常的；
    4. 素材改名前后都认得出来（和二次确认同一套放宽口径）；
    5. 选项没传（老前端、CLI）时行为完全不变——不能因为加了个开关就把别人带崩。
    """

    def setUp(self):
        super().setUp()
        app = Flask(__name__)
        app.register_blueprint(BP.publish_bp)
        self.client = app.test_client()

        # 基类只造了两个 type=1（小红书）的账号；「别的平台不受影响」那条要用抖音账号，
        # 否则提交会被"账号不属于这个平台"挡成 400。
        with records.db() as conn:
            conn.execute(
                "INSERT INTO user_info (id, userName, type, filePath) VALUES (3, '抖音账号', 3, 'c.json')"
            )

        self._saved = {
            "tasks": dict(BP._tasks),
            "jobs": dict(BP._jobs),
            "waiting": {k: list(v) for k, v in BP._waiting.items()},
            "queues": dict(BP._queues),
        }
        BP._tasks.clear()
        BP._jobs.clear()
        BP._waiting.clear()
        BP._queues = __import__("collections").defaultdict(queue.Queue)
        self._patches = [patch.object(BP, "_ensure_worker", lambda channel: None)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        BP._tasks.clear()
        BP._tasks.update(self._saved["tasks"])
        BP._jobs.clear()
        BP._jobs.update(self._saved["jobs"])
        BP._waiting.clear()
        BP._waiting.update(self._saved["waiting"])
        BP._queues = self._saved["queues"]
        super().tearDown()

    def submit(self, materials, targets, *, skip=None):
        payload = {
            "items": [{"material": name, "title": f"{name} 的标题"} for name in materials],
            "targets": [{"platform": p, "account_ids": list(ids)} for p, ids in targets],
            "options": {},
        }
        if skip is not None:
            payload["skip_published"] = skip
        res = self.client.post("/publish/submit", json=payload)
        self.assertEqual(res.status_code, 200)
        return res.get_json()["data"]

    def created(self, data):
        """实际建出来的任务：`{素材: {平台}}`。"""
        out: dict[str, set] = {}
        if not data.get("job_id"):
            return out
        with BP._lock:
            for task_id in BP._jobs[data["job_id"]]["task_ids"]:
                task = BP._tasks[task_id]
                out.setdefault(task["material"], set()).add(int(task["platform"]))
        return out

    # ---- 1. 按平台跳过 ---- #
    def test_published_material_is_skipped_on_that_platform(self):
        self.record("demo.mp4", platform=1)

        data = self.submit(["demo.mp4"], [(1, [1])], skip=True)

        self.assertEqual(data["task_count"], 0)
        self.assertEqual(data["skipped_count"], 1)
        self.assertEqual(data["skipped"][0]["material"], "demo.mp4")
        self.assertEqual(data["skipped"][0]["platform"], 1)
        self.assertEqual(self.created(data), {})

    def test_skip_covers_every_account_on_that_platform(self):
        """只要这个平台上有一个账号发成功过，整个平台跳过（不是只跳那个账号）。"""
        self.record("demo.mp4", platform=1, account_id=2, account_name="账号B")

        data = self.submit(["demo.mp4"], [(1, [1, 2])], skip=True)

        self.assertEqual(data["task_count"], 0)
        # 2 个账号 → 2 条任务都被跳过
        self.assertEqual(data["skipped_count"], 2)

    def test_other_platforms_still_publish(self):
        self.record("demo.mp4", platform=1)

        data = self.submit(["demo.mp4"], [(1, [1]), (3, [3])], skip=True)

        self.assertEqual(data["task_count"], 1)
        self.assertEqual(self.created(data), {"demo.mp4": {3}})
        self.assertEqual([s["platform"] for s in data["skipped"]], [1])

    def test_unpublished_material_in_the_same_batch_still_publishes(self):
        self.record("发过的.mp4", platform=1)

        data = self.submit(["发过的.mp4", "新的.mp4"], [(1, [1])], skip=True)

        self.assertEqual(self.created(data), {"新的.mp4": {1}})
        self.assertEqual(data["skipped_count"], 1)

    # ---- 2. 只有 success 算数 ---- #
    def test_failed_or_interrupted_records_do_not_skip(self):
        self.record("demo.mp4", platform=1, status=records.STATUS_FAILED)
        self.record("demo.mp4", platform=1, status=records.STATUS_CANCELLED)
        self.record("demo.mp4", platform=1, status=records.STATUS_INTERRUPTED)

        data = self.submit(["demo.mp4"], [(1, [1])], skip=True)

        self.assertEqual(data["task_count"], 1)
        self.assertEqual(data["skipped_count"], 0)

    def test_in_flight_records_do_not_skip(self):
        """在飞的不算"已发布成功"：这一批刚提交完再点一次，不该把自己当成重复。"""
        self.record("demo.mp4", platform=1, status=records.STATUS_RUNNING)

        data = self.submit(["demo.mp4"], [(1, [1])], skip=True)

        self.assertEqual(data["task_count"], 1)
        self.assertEqual(data["skipped_count"], 0)

    # ---- 3. 改名 ---- #
    def test_matches_after_material_was_renamed(self):
        """素材生成 AI 文案时会被改名，记录里存的是发布当时的名字。"""
        self.record("uuid-1234_海边日落.mp4", material_title="海边日落.mp4", platform=1)

        data = self.submit(["海边日落.mp4"], [(1, [1])], skip=True)

        self.assertEqual(data["task_count"], 0)
        self.assertEqual(data["skipped_count"], 1)

    # ---- 4. 不传 / 传 false 时行为不变 ---- #
    def test_option_absent_keeps_old_behaviour(self):
        self.record("demo.mp4", platform=1)

        data = self.submit(["demo.mp4"], [(1, [1])])

        self.assertEqual(data["task_count"], 1)
        self.assertEqual(data["skipped_count"], 0)

    def test_option_false_publishes_everything(self):
        """取消勾选 = 全部照发（用户明确要重发时就是这么用）。"""
        self.record("demo.mp4", platform=1)

        data = self.submit(["demo.mp4"], [(1, [1])], skip=False)

        self.assertEqual(data["task_count"], 1)
        self.assertEqual(data["skipped_count"], 0)

    # ---- 5. 整批全被跳过 ---- #
    def test_all_skipped_creates_no_job(self):
        """全跳过时不建空批次：前端据此提示"这批都发过了"，而不是开一个永远空的进度页。"""
        self.record("demo.mp4", platform=1)

        data = self.submit(["demo.mp4"], [(1, [1])], skip=True)

        self.assertEqual(data["job_id"], "")
        self.assertEqual(data["task_count"], 0)
        self.assertEqual(data["tasks"], [])
        self.assertEqual(data["skipped_count"], 1)


class CheckPublishedQueryTest(unittest.TestCase):
    """查不动的时候要**明确失败**，不能假装"没有重复"。

    前端把查询失败当成"没有重复"继续发布（重复发布最坏是多一条重复内容，卡住发布更糟），
    但那是前端的取舍。接口这边必须如实报错：一个 200 + 空列表的假数据会让"为什么没提醒我"
    变得没法查。
    """

    def test_endpoint_reports_database_errors_instead_of_faking_success(self):
        app = Flask(__name__)
        app.register_blueprint(BP.publish_bp)
        app.logger.disabled = True          # 这里就是要看它报错，别把 traceback 刷进测试输出
        client = app.test_client()
        with patch.object(records, "find_repeat_publishes", side_effect=sqlite3.OperationalError("no table")):
            res = client.post(
                "/publish/check-published",
                json={"items": [{"material": "a.mp4"}], "targets": [{"platform": 1, "account_ids": [1]}]},
            )
        self.assertEqual(res.status_code, 500)


if __name__ == "__main__":
    unittest.main()
