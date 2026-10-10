# -*- coding: utf-8 -*-
"""「账号管理」刷新时的登录态校验（`myUtils/auth.py::check_accounts` + `/getValidAccounts`）。

这次改造要守住的两件事，就是下面两组测试：

1. **各账号并发**，不是一个接一个排队。原来的写法是在 `/getValidAccounts` 里
   for 循环逐个 `await check_cookie`：每个账号都要开一次浏览器、等几个 5 秒级的超时，
   实测 5 个账号 **99.4 秒**（串行 = 5.8 + 9.0 + 26.6 + 28.8 + 29.2）。串行的真正代价
   不只是总时长——一个账号慢，后面所有账号都得跟着等，界面上就是"全部都在验证中"。

2. **"没校验成"不等于"失效"**。账号正在发布（拿不到账号锁）、页面超时、cookie 文件
   丢了……这些都只是"这次没查到"。它们**绝不能写库**把账号标成异常：那会把人骗去
   重新扫码，而重扫是破坏性的（旧登录态当场作废）。所以结论里 `ok=None` 和 `ok=False`
   必须分得清清楚楚。

测试全部把平台侧换掉（`check_cookie` 用假的），一个浏览器都不开、一个平台都不碰。
"""

import asyncio
import json
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from myUtils import auth
from myUtils.browser_lock import MAX_CONCURRENT_BROWSERS, account_lock


class _FakeCookieDir(unittest.TestCase):
    """把 `auth.COOKIE_DIR` 指到临时目录，并造出几个假的 cookie 文件。

    假文件是必须的：`_verify_one` 第一件事就是看 cookie 文件在不在，
    文件不在的账号压根不会走到"开浏览器"那一步。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cookie_dir = Path(self._tmp.name)
        patcher = patch.object(auth, "COOKIE_DIR", self.cookie_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_accounts(self, count: int) -> list[dict]:
        accounts = []
        for index in range(count):
            file_name = f"{index}.json"
            (self.cookie_dir / file_name).write_text(
                json.dumps({"cookies": [], "origins": []}), encoding="utf-8"
            )
            accounts.append(
                {
                    "id": 100 + index,
                    "type": 1,
                    "file_path": file_name,
                    "name": f"账号{index}",
                }
            )
        return accounts


class CheckAccountsConcurrencyTest(_FakeCookieDir):
    def test_accounts_are_verified_in_parallel(self):
        accounts = self.make_accounts(4)
        lock = threading.Lock()
        live = 0
        peak = 0

        async def fake_check_cookie(type_, file_path, save_state=False):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            await asyncio.sleep(0.5)  # 假装在开浏览器、等页面
            with lock:
                live -= 1
            return True

        with patch.object(auth, "check_cookie", fake_check_cookie):
            started = time.monotonic()
            results = auth.check_accounts(accounts)
            elapsed = time.monotonic() - started

        self.assertEqual([r["status"] for r in results], [auth.STATUS_OK] * 4)
        self.assertGreaterEqual(peak, 2, "四个账号必须同时在跑；峰值只有 1 就是又排成队了")
        # 串行至少 2.0 秒（4 × 0.5），并发只该是"最慢那个"那点时间。
        # 阈值留得宽：这里要挡的是"退化成串行"，不是去卡秒表。
        self.assertLess(elapsed, 1.0, f"总耗时 {elapsed:.2f}s，看着像还是串行的")

    def test_results_stay_aligned_when_they_finish_out_of_order(self):
        accounts = self.make_accounts(4)
        # 越靠前的账号睡得越久 → 完成顺序和入参顺序正好相反。
        # 用 pool.map 保序就是为了这个：调用方要拿第 i 条结论去更新第 i 个账号。
        delays = {account["file_path"]: 0.05 * (4 - index) for index, account in enumerate(accounts)}

        async def fake_check_cookie(type_, file_path, save_state=False):
            await asyncio.sleep(delays[file_path])
            return file_path.endswith("0.json")

        with patch.object(auth, "check_cookie", fake_check_cookie):
            results = auth.check_accounts(accounts)

        self.assertEqual([r["id"] for r in results], [a["id"] for a in accounts])
        self.assertEqual([r["name"] for r in results], [a["name"] for a in accounts])
        self.assertEqual(results[0]["status"], auth.STATUS_OK, "0.json 是有效的那个")
        self.assertEqual(results[-1]["status"], auth.STATUS_INVALID, "3.json 是失效的那个")

    def test_one_account_crashing_does_not_affect_the_others(self):
        accounts = self.make_accounts(3)

        async def fake_check_cookie(type_, file_path, save_state=False):
            if file_path == "1.json":
                raise RuntimeError("浏览器没起来")
            return True

        with patch.object(auth, "check_cookie", fake_check_cookie):
            results = auth.check_accounts(accounts)

        self.assertEqual([r["status"] for r in results],
                         [auth.STATUS_OK, auth.STATUS_ERROR, auth.STATUS_OK])
        self.assertIn("浏览器没起来", results[1]["message"], "出错原因要带给用户，不然没法排查")

    def test_busy_account_is_skipped_not_marked_invalid(self):
        """拿不到账号锁 = "这次没查到"，**不是**"登录态失效"。

        真按失效处理的话，一个正在发视频的账号点一次刷新就会被标成异常、
        弹着"重新扫码"的按钮——而这完全没有根据。

        用两个账号而不是一个：被占用那个不该拖住旁边那个，而**单账号那一路是直接
        在当前线程里跑的**（为了省掉一个只有一条任务的线程池），测不出锁竞争。
        """
        accounts = self.make_accounts(2)
        called = []

        async def fake_check_cookie(type_, file_path, save_state=False):
            called.append(file_path)
            return True

        held = account_lock(auth.COOKIE_DIR / accounts[0]["file_path"])
        held.acquire()  # 假装发布任务正拿着这个账号的锁
        try:
            with patch.object(auth, "check_cookie", fake_check_cookie):
                results = auth.check_accounts(accounts, wait_timeout=0)
        finally:
            held.release()

        self.assertEqual(results[0]["status"], auth.STATUS_BUSY)
        self.assertIsNone(results[0]["ok"], "没校验成的账号 ok 必须是 None——调用方靠它决定别写库")
        self.assertEqual(results[1]["status"], auth.STATUS_OK, "被占用的账号不该拖住别的账号")
        self.assertEqual(called, ["1.json"], "锁都被占了，就根本不该为它开浏览器")

    def test_missing_cookie_file_is_an_error_not_invalid(self):
        """文件丢了要重新扫码，但结论是 error 而不是 invalid：要能一眼分清原因。"""
        account = self.make_accounts(1)[0]
        (self.cookie_dir / account["file_path"]).unlink()

        results = auth.check_accounts([account])

        self.assertEqual(results[0]["status"], auth.STATUS_ERROR)
        self.assertIsNone(results[0]["ok"])
        self.assertIn("cookie 文件不存在", results[0]["message"])

    def test_more_accounts_than_browser_slots_still_all_get_verified(self):
        """账号比"整机同时几个浏览器"（默认 4）多的时候，多出来的那些是**在线程池里排队**，
        不是在闸门前干等——所以哪怕 `wait_timeout=0`（一点不等）也一个都不会漏。

        这条正是"线程数只开到浏览器名额那么多"的理由：线程开满 N 个的话，排在后面的
        会在 `browser_session` 门口耗到超时，然后被报成"这次没校验"。
        """
        total = MAX_CONCURRENT_BROWSERS + 2
        accounts = self.make_accounts(total)

        async def fake_check_cookie(type_, file_path, save_state=False):
            await asyncio.sleep(0.05)
            return True

        with patch.object(auth, "check_cookie", fake_check_cookie):
            results = auth.check_accounts(accounts, wait_timeout=0)

        self.assertEqual(len(results), total)
        self.assertEqual([r["status"] for r in results], [auth.STATUS_OK] * total)

    def test_empty_list_is_fine(self):
        self.assertEqual(auth.check_accounts([]), [])


def _admin_password() -> str:
    """读本机 settings.json 里的后台密码（接口测试要带 token 才能过鉴权守卫）。"""
    from conf import BASE_DIR

    path = Path(BASE_DIR) / "settings.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))["auth"]["password"]
    except Exception:  # noqa: BLE001 —— 读不到就跳过，别把测试搞成"环境不对就红"
        return ""


@contextmanager
def _db_in_tmpdir(rows: list[tuple]):
    """把后端读的那个 `db/database.db` 换成临时库。

    往真库里塞测试账号是不能接受的（那正是用户每天在用的那份数据）。后端那几处
    写死了 `BASE_DIR/db/database.db`，所以这里只把"连的是哪个文件"换掉。
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "database.db"
        real_connect = sqlite3.connect
        conn = real_connect(db_path)
        conn.execute('''
            CREATE TABLE user_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type INTEGER NOT NULL,
                filePath TEXT NOT NULL,
                userName TEXT NOT NULL,
                status INTEGER DEFAULT 0
            )''')
        conn.executemany(
            "INSERT INTO user_info (id, type, filePath, userName, status) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        conn.close()

        def fake_connect(*args, **kwargs):
            return real_connect(db_path, **kwargs)

        with patch.object(sqlite3, "connect", fake_connect):
            yield db_path


class WebEndpointTest(unittest.TestCase):
    """真的打一遍 `/getValidAccounts`（带鉴权），确认写库那条约定在路由上也成立。"""

    def setUp(self):
        import sau_backend

        self.backend = sau_backend
        self.client = sau_backend.app.test_client()
        password = _admin_password()
        if not password:
            self.skipTest("settings.json 里没有后台密码，跳过需要鉴权的接口测试")
        response = self.client.post("/auth/login", json={"username": "admin", "password": password})
        token = (response.get_json() or {}).get("data", {}).get("token")
        if not token:
            self.skipTest("后台登录失败，跳过需要鉴权的接口测试")
        self.headers = {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _result(account_id, name, status):
        return {
            "id": account_id,
            "type": 1,
            "file_path": f"{account_id}.json",
            "name": name,
            "ok": None if status != "invalid" else False,
            "status": status,
            "message": status,
        }

    def test_only_invalid_accounts_are_written_to_the_db(self):
        """三条结论 → 库里只有"确实失效"那条被改成 0。

        ok / busy / error 三条都不许改状态：ok 是"通过不写 1"的老约定
        （见 session_renew._mark_invalid），busy / error 是"这次压根没查到"。
        """
        rows = [(1, 1, "1.json", "甲", 1), (2, 3, "2.json", "乙", 1), (3, 4, "3.json", "丙", 1)]
        with _db_in_tmpdir(rows) as db_path:
            results = [self._result(1, "甲", "ok"),
                       self._result(2, "乙", "invalid"),
                       self._result(3, "丙", "busy")]
            with patch.object(self.backend, "check_accounts", return_value=results):
                response = self.client.get("/getValidAccounts", headers=self.headers)

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            conn = sqlite3.connect(db_path)
            try:
                statuses = dict(conn.execute("SELECT id, status FROM user_info"))
            finally:
                conn.close()

        self.assertEqual(statuses, {1: 1, 2: 0, 3: 1}, "只有失效的账号该被标成 0")
        # 返回给前端的行也要跟着变：不然界面上还是「正常」，用户以为没生效
        self.assertEqual([row[4] for row in payload["data"]], [1, 0, 1])
        self.assertEqual(payload["verify"]["invalid"], 1)
        self.assertEqual(payload["verify"]["busy"], 1)
        self.assertEqual([r["status"] for r in payload["verify"]["results"]], ["ok", "invalid", "busy"])

    def test_id_filters_to_a_single_account(self):
        rows = [(1, 1, "1.json", "甲", 1), (2, 3, "2.json", "乙", 1)]
        seen = []

        def fake_check_accounts(accounts, **kwargs):
            seen.append([a["id"] for a in accounts])
            return [self._result(2, "乙", "invalid")]

        with _db_in_tmpdir(rows):
            with patch.object(self.backend, "check_accounts", fake_check_accounts):
                response = self.client.get("/getValidAccounts?id=2", headers=self.headers)

        self.assertEqual(seen, [[2]], "带 id 就该只把那个账号交出去校验")
        self.assertEqual([row[0] for row in response.get_json()["data"]], [2], "data 也只回这一个账号")

    def test_non_numeric_id_is_rejected(self):
        """`?id=abc` 悄悄当成"查全部"是最坏的结果：调用方以为只查一个，
        实际把所有浏览器都开了一遍。"""
        with _db_in_tmpdir([(1, 1, "1.json", "甲", 1)]):
            with patch.object(self.backend, "check_accounts") as fake:
                response = self.client.get("/getValidAccounts?id=abc", headers=self.headers)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(fake.called, "参数都不合法，就不该去碰任何账号")

    def test_unknown_account_returns_404(self):
        with _db_in_tmpdir([(1, 1, "1.json", "甲", 1)]):
            with patch.object(self.backend, "check_accounts") as fake:
                response = self.client.get("/getValidAccounts?id=999", headers=self.headers)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(fake.called)

    def test_route_is_a_plain_sync_view(self):
        """改造前是 `async def` + 逐个 await。现在并发的活交给线程池，
        视图本身是普通的同步视图——别哪天又改回去，那等于把两个事件循环叠在一起。"""
        view = self.backend.app.view_functions["getValidAccounts"]
        self.assertFalse(asyncio.iscoroutinefunction(view), "视图不该再是 async def")


def _js_block(source: str, header: str) -> str:
    """取出 `header` 所在那个块的完整源码（从 header 到它配对的 `}`），并去掉行注释。

    够用就好的"解析"：这些块里的 `{` `}` 都是配对的（`` `${...}` `` 也算一对），
    所以数花括号就能切出来。要守住的是"别再往里塞一次账号校验"，
    不是写一个 JS 解析器。

    **必须去掉注释**：注释里会写"原来这里调 fetchAccounts()"这种说明，
    它们不该被当成"代码里真的调了"。
    """
    start = source.index(header)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return re.sub(r"//[^\n]*", "", source[start:index + 1])
    raise AssertionError(f"{header} 后面没找到配对的 }}")


class FrontendVerifyTriggersTest(unittest.TestCase):
    """「账号管理」页**什么时候**才去校验账号——这条改过两回，用源码钉死。

    为什么用源码断言而不是跑前端：这个仓库没有前端测试框架（`package.json` 里
    只有 vite），而这件事的代价很直观——进页面校验一次，用户每次点开都要白等
    99 秒 / 5 个账号。同类做法见 `tests/test_publish_records_startup.py`、
    `tests/test_douyin_sms_prompt.py`。
    """

    @classmethod
    def setUpClass(cls):
        from conf import BASE_DIR

        cls.source = (
            Path(BASE_DIR) / "sau_frontend" / "src" / "views" / "AccountManagement.vue"
        ).read_text(encoding="utf-8")

    #: 任何一条出现在"不该校验"的块里都算回归
    _VERIFY_CALLS = ("getValidAccounts", "verifyOneAccount", "refreshOneAccount", "fetchAccounts(")

    def test_entering_the_page_only_reads_the_db(self):
        body = _js_block(self.source, "onMounted(")
        self.assertIn("fetchAccountsQuick", body)
        for forbidden in self._VERIFY_CALLS:
            self.assertNotIn(forbidden, body, f"进页面不该校验账号，这里出现了 {forbidden}")

    def test_login_success_only_reads_the_db(self):
        """扫码成功后只读库：登录流程本身已经带着这个账号进过后台了。

        再开一个浏览器去"确认它有效"是把同一件事做第二遍（新账号 ~5 秒、
        视频号 ~30 秒），而状态和账号数据后端都已经写好了。
        """
        body = _js_block(self.source, "const refreshAccountsAfterLogin")
        self.assertIn("fetchAccountsQuick", body)
        for forbidden in self._VERIFY_CALLS:
            self.assertNotIn(forbidden, body, f"扫码成功不该校验账号，这里出现了 {forbidden}")

    def test_rename_does_not_verify(self):
        """改名只动 userName 一列，登录态没变——更不该把所有账号各开一次浏览器。"""
        body = _js_block(self.source, "const submitAccountForm")
        self.assertNotIn("fetchAccounts(", body)
        self.assertNotIn("refreshOneAccount", body)

    def test_refresh_button_still_verifies_everything(self):
        """六个 tab 各一个刷新按钮，都指向 fetchAccounts；它才是全量校验的入口。"""
        self.assertEqual(
            self.source.count('@click="fetchAccounts"'), 6, "每个 tab 都该有一个刷新按钮指向它"
        )
        self.assertIn("verifyOneAccount", _js_block(self.source, "const fetchAccounts ="))

    def test_swapping_a_cookie_only_verifies_that_one_account(self):
        body = _js_block(self.source, "const handleUploadCookie")
        self.assertIn("refreshOneAccount(row)", body)
        self.assertNotIn("fetchAccounts(", body)


if __name__ == "__main__":
    unittest.main()
