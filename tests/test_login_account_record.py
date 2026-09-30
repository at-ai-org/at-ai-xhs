"""扫码登录后的账号入库必须是**幂等**的（myUtils/login.py 的 `_record_account`）。

由来（真实 bug）：四个平台原来各写一句裸 `INSERT INTO user_info`，没有去重。
于是「重新扫码」不会更新原记录，而是插出一条新记录，旧的那条继续留着报「异常」——
界面上就出现两个同名账号（旧记录的 cookie 其实已经死了）。

更麻烦的是 `publish_records.account_id` 引用着 `user_info.id`：如果修复时"删旧留新"，
历史发布记录会被判定成「账号不存在（可能已被删除）」（见 sau_backend_publish.py 的校验）。
所以去重必须**保留 id 最小的那条**，只把 filePath 指到新 cookie。
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import myUtils.login as login


class RecordAccountTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        (self.base / "db").mkdir(parents=True, exist_ok=True)
        self.db_path = self.base / "db" / "database.db"
        self._patch = patch.object(login, "BASE_DIR", str(self.base))
        self._patch.start()
        self._create_tables(with_publish_records=True)

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _create_tables(self, *, with_publish_records: bool) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS user_info (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       type INTEGER NOT NULL,
                       filePath TEXT NOT NULL,
                       userName TEXT NOT NULL,
                       status INTEGER DEFAULT 0)"""
            )
            if with_publish_records:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS publish_records (
                           id INTEGER PRIMARY KEY AUTOINCREMENT,
                           account_id INTEGER,
                           account_name TEXT)"""
                )

    # ---- 查询辅助 ---- #
    def _accounts(self, platform_type=None):
        q = "SELECT id, type, filePath, userName, status FROM user_info"
        args = ()
        if platform_type is not None:
            q += " WHERE type = ?"
            args = (platform_type,)
        with sqlite3.connect(self.db_path) as conn:
            return [tuple(r) for r in conn.execute(q + " ORDER BY id", args).fetchall()]

    def _seed_account(self, platform_type, name, cookie, status=0):
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO user_info (type, filePath, userName, status) VALUES (?, ?, ?, ?)",
                (platform_type, cookie, name, status),
            )
            return int(cur.lastrowid)

    def _seed_publish_record(self, account_id, name="x"):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO publish_records (account_id, account_name) VALUES (?, ?)",
                (account_id, name),
            )

    def _publish_account_ids(self):
        with sqlite3.connect(self.db_path) as conn:
            return sorted(r[0] for r in conn.execute("SELECT account_id FROM publish_records"))


class NewAccountTest(RecordAccountTestBase):
    def test_inserts_when_no_existing_row(self):
        account_id, created = login._record_account(4, "new.json", "麻辣土豆")
        self.assertTrue(created)
        self.assertEqual(
            self._accounts(), [(account_id, 4, "new.json", "麻辣土豆", 1)]
        )


class IdempotencyTest(RecordAccountTestBase):
    def test_rescan_updates_instead_of_duplicating(self):
        """核心回归：同一个平台同名账号重新扫码，只能更新，不能变成两条。"""
        old_id = self._seed_account(4, "麻辣土豆", "dead.json", status=0)

        new_id, created = login._record_account(4, "fresh.json", "麻辣土豆")

        self.assertFalse(created, "重新扫码不该新建记录")
        self.assertEqual(new_id, old_id, "必须沿用原来的 id，否则历史发布记录会断链")
        self.assertEqual(self._accounts(), [(old_id, 4, "fresh.json", "麻辣土豆", 1)])
        self.assertEqual(len(self._accounts()), 1, "不能出现两个同名账号")

    def test_update_refreshes_status_to_ok(self):
        self._seed_account(2, "麻辣香锅", "dead.json", status=0)
        login._record_account(2, "fresh.json", "麻辣香锅")
        self.assertEqual(self._accounts()[0][4], 1, "重新扫码成功后状态要回到正常")

    def test_same_name_on_different_platforms_stay_separate(self):
        """不同平台的同名账号是两回事，不能互相覆盖。"""
        login._record_account(4, "ks.json", "同一个名字")
        login._record_account(3, "dy.json", "同一个名字")
        self.assertEqual(len(self._accounts()), 2)

    def test_different_names_on_same_platform_stay_separate(self):
        """同一平台的两个不同账号必须都能存在——去重不能误伤。"""
        login._record_account(4, "a.json", "快手A")
        login._record_account(4, "b.json", "快手B")
        self.assertEqual(sorted(a[3] for a in self._accounts()), ["快手A", "快手B"])


class DuplicateMergeTest(RecordAccountTestBase):
    def test_merges_existing_duplicates_keeping_lowest_id(self):
        """库里已经有重复行（历史 bug 留下的）时，下次扫码应自动合并。"""
        keep = self._seed_account(4, "麻辣土豆", "dead.json", status=0)
        dup = self._seed_account(4, "麻辣土豆", "also-dead.json", status=0)

        new_id, created = login._record_account(4, "fresh.json", "麻辣土豆")

        self.assertFalse(created)
        self.assertEqual(new_id, keep, "保留 id 最小的那条（原先就存在的）")
        self.assertEqual(self._accounts(), [(keep, 4, "fresh.json", "麻辣土豆", 1)])
        self.assertNotIn(dup, [a[0] for a in self._accounts()])

    def test_merge_repoints_publish_records_of_removed_duplicate(self):
        """被删掉的重复行若有历史发布记录，要先改指到保留行，不能断链。"""
        keep = self._seed_account(4, "麻辣土豆", "dead.json")
        dup = self._seed_account(4, "麻辣土豆", "also-dead.json")
        self._seed_publish_record(keep)
        self._seed_publish_record(dup)
        self._seed_publish_record(dup)

        login._record_account(4, "fresh.json", "麻辣土豆")

        self.assertEqual(
            self._publish_account_ids(), [keep, keep, keep], "历史记录必须全部指向保留的那条"
        )

    def test_merge_survives_missing_publish_records_table(self):
        """老库可能还没有 publish_records 表——合并不能因此把登录流程搞崩。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DROP TABLE publish_records")

        self._seed_account(4, "麻辣土豆", "dead.json")
        self._seed_account(4, "麻辣土豆", "also-dead.json")

        account_id, created = login._record_account(4, "fresh.json", "麻辣土豆")

        self.assertFalse(created)
        self.assertEqual(len(self._accounts()), 1)
        self.assertEqual(self._accounts()[0][2], "fresh.json")


class CrossPlatformCookieRoutingTest(RecordAccountTestBase):
    def test_each_platform_records_its_own_type(self):
        """四个平台的调用点各自传对自己那个 type，别串号（1 小红书 2 视频号 3 抖音 4 快手）。"""
        login._record_account(1, "xhs.json", "小红书号")
        login._record_account(2, "tx.json", "视频号号")
        login._record_account(3, "dy.json", "抖音号")
        login._record_account(4, "ks.json", "快手号")
        self.assertEqual(
            sorted((a[1], a[3]) for a in self._accounts()),
            [(1, "小红书号"), (2, "视频号号"), (3, "抖音号"), (4, "快手号")],
        )


if __name__ == "__main__":
    unittest.main()
