"""启动时的数据修正必须**只**发生在服务进程启动那一刻。

由来（真实事故）：这些修正原先挂在 `ensure_tables()` 里，而 `ensure_tables()` 在
**模块 import 时**执行。结果是——任何 `import myUtils.publish_records` 的脚本
（调试命令、维护工具、REPL）都会顺手把**正在发布的那条任务**判成「已中断」。
我就用一条验证命令真的把你的库改成那样了。

所以现在的分工是：
* `ensure_tables()`   —— 纯 DDL，随便 import，一个字节业务数据都不改；
* `init_on_startup()` —— 唯一的数据修正入口，只有 `sau_backend.py` 的 `__main__` 调它。
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import myUtils.publish_records as records


class RecordsStartupTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "database.db"
        self._patch = patch.object(records, "DB_PATH", self.db_path)
        self._patch.start()
        records.ensure_tables()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    # ---- 辅助 ---- #
    def _insert(self, status, source=None, rid=None):
        with records.db() as conn:
            cur = conn.execute(
                "INSERT INTO publish_records (status, source, platform, account_name, created_at)"
                " VALUES (?, ?, 1, 'acc', '2026-09-28 14:00:00')",
                (status, source),
            )
            return int(cur.lastrowid)

    def _statuses(self):
        with records.db() as conn:
            return {
                row["id"]: row["status"]
                for row in conn.execute("SELECT id, status FROM publish_records ORDER BY id")
            }

    def _row(self, rid):
        with records.db() as conn:
            return dict(
                conn.execute("SELECT * FROM publish_records WHERE id = ?", (rid,)).fetchone()
            )


class EnsureTablesIsSchemaOnlyTest(RecordsStartupTestBase):
    def test_ensure_tables_does_not_touch_business_data(self):
        """核心回归：建表函数绝不能改任务状态——它会在 import 时执行。"""
        running = self._insert(records.STATUS_RUNNING)
        queued = self._insert(records.STATUS_QUEUED)

        records.ensure_tables()  # 模拟 import（也模拟被反复调用）

        self.assertEqual(
            self._statuses(),
            {running: records.STATUS_RUNNING, queued: records.STATUS_QUEUED},
            "ensure_tables 不该把还在跑的任务标成中断",
        )

    def test_ensure_tables_does_not_backfill_source(self):
        """source 回填也是数据写入，同样不该挂在 import 上。"""
        rid = self._insert(records.STATUS_SUCCESS, source=None)
        records.ensure_tables()
        self.assertEqual(self._row(rid)["source"], None)

    def test_source_of_truth_is_the_call_site(self):
        """把"只有服务进程 __main__ 该做数据修正"钉在测试里。

        这条断言很土，但它防的是本次事故的复发方式：把数据修正又挪回
        `ensure_tables()` 里"顺手做了"。那样任何 import 都会误伤正在跑的任务。
        """
        import inspect

        from conf import BASE_DIR

        # 建表函数里不许出现任何数据写入
        self.assertNotIn("UPDATE publish_records", inspect.getsource(records.ensure_tables))

        # 数据修正的调用点必须在服务进程的 __main__ 里（不 import 整个 Flask 应用，
        # 直接看源码文本即可——真跑起来会拉起一堆上传器，没必要）
        backend_src = (Path(BASE_DIR) / "sau_backend.py").read_text(encoding="utf-8")
        main_block = backend_src.split("if __name__ == '__main__':", 1)
        self.assertEqual(len(main_block), 2, "sau_backend.py 应当有 __main__ 入口")
        self.assertIn("init_on_startup", main_block[1], "启动收尾必须挂在服务进程入口上")
        # import 阶段（__main__ 之前）不许调用它
        self.assertNotIn("init_on_startup(", main_block[0])


class MarkOrphansTest(RecordsStartupTestBase):
    def test_marks_only_queued_and_running(self):
        running = self._insert(records.STATUS_RUNNING)
        queued = self._insert(records.STATUS_QUEUED)
        success = self._insert(records.STATUS_SUCCESS)
        failed = self._insert(records.STATUS_FAILED)
        cancelled = self._insert(records.STATUS_CANCELLED)

        changed = records.mark_orphan_records_interrupted()

        self.assertEqual(changed, 2, "只该动 queued/running 两条")
        self.assertEqual(
            self._statuses(),
            {
                running: records.STATUS_INTERRUPTED,
                queued: records.STATUS_INTERRUPTED,
                success: records.STATUS_SUCCESS,
                failed: records.STATUS_FAILED,
                cancelled: records.STATUS_CANCELLED,
            },
        )

    def test_records_when_and_why(self):
        """中断的记录要留下时间和原因，否则用户在"发布记录"里只看到一条没有解释的失败。"""
        rid = self._insert(records.STATUS_RUNNING)
        records.mark_orphan_records_interrupted()

        row = self._row(rid)
        self.assertTrue(row["finished_at"], "应当补上结束时间")
        self.assertIn("后端进程重启", row["message"])
        self.assertEqual(row["stage_text"], records.STATUS_TEXT[records.STATUS_INTERRUPTED])

    def test_custom_reason_wins(self):
        rid = self._insert(records.STATUS_RUNNING)
        records.mark_orphan_records_interrupted(reason="看门狗判定卡住")
        self.assertEqual(self._row(rid)["message"], "看门狗判定卡住")

    def test_returns_zero_when_nothing_to_do(self):
        self._insert(records.STATUS_SUCCESS)
        self.assertEqual(records.mark_orphan_records_interrupted(), 0)


class InitOnStartupTest(RecordsStartupTestBase):
    def test_does_both_jobs_and_reports_counts(self):
        # 给僵尸记录写明来源，好让两条断言互不干扰（否则它自己也会被回填 source）
        orphan = self._insert(records.STATUS_RUNNING, source=records.SOURCE_PUBLISH_CENTER)
        legacy = self._insert(records.STATUS_SUCCESS, source=None)

        result = records.init_on_startup()

        self.assertEqual(result["orphans_interrupted"], 1)
        self.assertEqual(result["source_backfilled"], 1)
        self.assertEqual(self._row(orphan)["status"], records.STATUS_INTERRUPTED)
        self.assertEqual(self._row(legacy)["source"], records.SOURCE_PUBLISH_CENTER)

    def test_idempotent(self):
        """反复调用不能产生副作用（服务重启、守护进程拉起都会跑它）。"""
        self._insert(records.STATUS_RUNNING)
        first = records.init_on_startup()
        second = records.init_on_startup()
        self.assertEqual(first["orphans_interrupted"], 1)
        self.assertEqual(second["orphans_interrupted"], 0)
        self.assertEqual(second["source_backfilled"], 0)

    def test_backfill_leaves_explicit_source_alone(self):
        """已经写明来源的记录不该被改写成发布中心。"""
        rid = self._insert(records.STATUS_SUCCESS, source=records.SOURCE_XHS_WORKBENCH)
        records.init_on_startup()
        self.assertEqual(self._row(rid)["source"], records.SOURCE_XHS_WORKBENCH)


if __name__ == "__main__":
    unittest.main()
