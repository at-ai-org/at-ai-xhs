"""账号会话定时续期（myUtils/session_renew.py + 设置里的 session_renew 段）。

这个功能最容易出的不是"跑不起来"，而是**静默地不跑**：开关显示"开启"、
界面上一片正常，但到点什么都没发生。所以这里的重点全在"该跑的时候一定跑，
不该重复跑的时候不重复跑"：

* 时间点归一化——`"7:00"` 这种写法会让 `now.strftime("%H:%M") in times` 永远为假；
* 同一 (日期, 时间点) 只触发一次——否则整分钟里每 30 秒触发一遍；
* 该账号正在发布时**跳过而不是排队**——续期只是保活，不该把发布堵住；
* 设置的合并语义——改开关别把时间点冲掉，传坏时间点别把配置清空。
"""

import asyncio
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from myUtils import app_settings, session_renew
from myUtils.browser_lock import BrowserLockTimeout, account_lock, browser_session


@contextmanager
def _settings_in_tmpdir():
    """把 settings.json 指到临时目录，别动本机那份真配置。"""
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(app_settings, "SETTINGS_PATH", Path(tmp) / "settings.json"):
            yield Path(tmp) / "settings.json"


class SessionRenewSettingsTest(unittest.TestCase):
    def test_default_is_enabled_with_the_four_slots(self):
        with _settings_in_tmpdir():
            cfg = app_settings.get_session_renew_config()
        self.assertTrue(cfg["enabled"], "默认必须是开启的")
        self.assertEqual(cfg["times"], ["00:00", "07:00", "14:00", "20:00"])

    def test_old_settings_file_without_the_section_still_gets_defaults(self):
        """老 settings.json 里没有 session_renew 段，读出来也要是"默认开启"。"""
        with _settings_in_tmpdir() as path:
            path.write_text('{"default_collection": "风景"}', encoding="utf-8")
            cfg = app_settings.get_session_renew_config()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["times"], ["00:00", "07:00", "14:00", "20:00"])

    def test_times_are_normalized_deduped_and_sorted(self):
        self.assertEqual(
            app_settings.normalize_renew_times(["7:00", " 20:30 ", "00:00", "7:00"]),
            ["00:00", "07:00", "20:30"],
        )

    def test_unparsable_times_are_dropped_not_guessed(self):
        """认不出来的一律丢掉——宁可少一个点，也不要猜出一个用户没想到的时间。"""
        self.assertEqual(app_settings.normalize_renew_times(["0700", "7点", "25:00", "12:60"]), [])

    def test_comma_separated_string_is_accepted(self):
        self.assertEqual(app_settings.normalize_renew_times("0:00, 7:00 14:00"), ["00:00", "07:00", "14:00"])

    def test_cap_on_number_of_times(self):
        many = [f"{h:02d}:00" for h in range(24)] + ["23:30"]
        self.assertEqual(len(app_settings.normalize_renew_times(many)), app_settings.MAX_SESSION_RENEW_TIMES)

    def test_toggling_off_keeps_the_times(self):
        with _settings_in_tmpdir():
            app_settings.save_settings({"session_renew": {"enabled": False}})
            cfg = app_settings.get_session_renew_config()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["times"], ["00:00", "07:00", "14:00", "20:00"])

    def test_changing_times_keeps_the_switch(self):
        with _settings_in_tmpdir():
            app_settings.save_settings({"session_renew": {"enabled": False}})
            app_settings.save_settings({"session_renew": {"times": ["09:30"]}})
            cfg = app_settings.get_session_renew_config()
        self.assertFalse(cfg["enabled"], "只改时间点不该把开关又打开")
        self.assertEqual(cfg["times"], ["09:30"])

    def test_garbage_times_do_not_wipe_the_configuration(self):
        """传一堆认不出来的时间点时保留原值。

        清空的话开关还显示"开启"，但一个点都不跑——界面上完全看不出来，
        这正是这个功能最怕的失败方式。要停请关开关。
        """
        with _settings_in_tmpdir():
            app_settings.save_settings({"session_renew": {"times": ["06:00"]}})
            app_settings.save_settings({"session_renew": {"times": ["乱七八糟", None]}})
            cfg = app_settings.get_session_renew_config()
        self.assertEqual(cfg["times"], ["06:00"])

    def test_truthy_non_dict_patch_is_read_as_the_switch(self):
        with _settings_in_tmpdir():
            app_settings.save_settings({"session_renew": False})
            self.assertFalse(app_settings.get_session_renew_config()["enabled"])
            app_settings.save_settings({"session_renew": True})
            self.assertTrue(app_settings.get_session_renew_config()["enabled"])

    def test_settings_are_visible_to_the_frontend(self):
        """设置页/账号页都从 public_settings 里读，别只存在后端。"""
        with _settings_in_tmpdir():
            public = app_settings.public_settings()
        self.assertIn("session_renew", public)
        self.assertTrue(public["session_renew"]["enabled"])


class DueSlotTest(unittest.TestCase):
    CONFIG = {"enabled": True, "times": ["00:00", "07:00", "14:00", "20:00"]}

    def test_fires_on_the_configured_minute(self):
        now = datetime(2026, 9, 30, 7, 0, 12)
        self.assertEqual(session_renew.plan_due_slots(self.CONFIG, now, set()), ["07:00"])

    def test_does_not_fire_on_other_minutes(self):
        for hour, minute in ((6, 59), (7, 1), (13, 59)):
            with self.subTest(hour=hour, minute=minute):
                now = datetime(2026, 9, 30, hour, minute, 0)
                self.assertEqual(session_renew.plan_due_slots(self.CONFIG, now, set()), [])

    def test_only_fires_once_per_day_per_slot(self):
        """主循环每 30 秒醒一次，同一分钟里会进来两次——第二次不能再触发。"""
        now = datetime(2026, 9, 30, 7, 0, 5)
        first = session_renew.plan_due_slots(self.CONFIG, now, set())
        self.assertEqual(first, ["07:00"])
        done = {(now.date().isoformat(), "07:00")}
        # 同一分钟内（30 秒后）再 tick 一次
        self.assertEqual(session_renew.plan_due_slots(self.CONFIG, now, done), [])

    def test_next_day_fires_again(self):
        done = {("2026-09-30", "07:00")}
        tomorrow = datetime(2026, 10, 1, 7, 0, 3)
        self.assertEqual(session_renew.plan_due_slots(self.CONFIG, tomorrow, done), ["07:00"])

    def test_disabled_never_fires(self):
        config = {"enabled": False, "times": ["07:00"]}
        now = datetime(2026, 9, 30, 7, 0, 0)
        self.assertEqual(session_renew.plan_due_slots(config, now, set()), [])

    def test_exactly_one_time_matches_any_single_minute(self):
        """同一个时刻最多命中一个时间点（时间点本身是去重过的）。"""
        config = {"enabled": True, "times": ["07:00", "07:00", "20:00"]}
        now = datetime(2026, 9, 30, 7, 0, 0)
        self.assertEqual(session_renew.plan_due_slots(config, now, set()), ["07:00"])


class NextRunTest(unittest.TestCase):
    CONFIG = {"enabled": True, "times": ["00:00", "07:00", "14:00", "20:00"]}

    def test_next_run_is_today_when_a_slot_is_still_ahead(self):
        upcoming = session_renew.next_run_at(self.CONFIG, datetime(2026, 9, 30, 13, 0))
        self.assertEqual(upcoming, datetime(2026, 9, 30, 14, 0))

    def test_next_run_rolls_to_tomorrow_after_the_last_slot(self):
        upcoming = session_renew.next_run_at(self.CONFIG, datetime(2026, 9, 30, 23, 30))
        self.assertEqual(upcoming, datetime(2026, 10, 1, 0, 0))

    def test_no_next_run_when_disabled(self):
        config = {"enabled": False, "times": ["07:00"]}
        self.assertIsNone(session_renew.next_run_at(config, datetime(2026, 9, 30, 6, 0)))


class RenewAccountTest(unittest.TestCase):
    ACCOUNT = {"id": 7, "type": 3, "file_path": "acc.json", "name": "账号A", "platform": "抖音"}

    def _with_cookie_file(self, tmp: str) -> Path:
        cookie_dir = Path(tmp)
        (cookie_dir / "acc.json").write_text("[]", encoding="utf-8")
        return cookie_dir

    def test_missing_cookie_file_is_reported_not_crashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(session_renew, "COOKIE_DIR", Path(tmp)):
                row = session_renew.renew_account(self.ACCOUNT)
        self.assertEqual(row["status"], session_renew.STATUS_ERROR)
        self.assertIn("重新扫码", row["message"])

    def test_valid_session_is_renewed_and_new_tickets_are_kept(self):
        """续期必须带 save_state=True：不回写的话服务端轮换的新票据就丢了。"""
        calls = {}

        async def fake_check_cookie(type_, file_path, save_state=False):
            calls.update({"type": type_, "file": file_path, "save_state": save_state})
            return True

        with tempfile.TemporaryDirectory() as tmp:
            cookie_dir = self._with_cookie_file(tmp)
            with patch.object(session_renew, "COOKIE_DIR", cookie_dir), patch.object(
                session_renew, "check_cookie", fake_check_cookie
            ):
                row = session_renew.renew_account(self.ACCOUNT)

        self.assertEqual(row["status"], session_renew.STATUS_OK)
        self.assertEqual(calls, {"type": 3, "file": "acc.json", "save_state": True})

    def test_invalid_session_marks_the_account_in_the_database(self):
        marked = []

        async def fake_check_cookie(*_args, **_kwargs):
            return False

        with tempfile.TemporaryDirectory() as tmp:
            cookie_dir = self._with_cookie_file(tmp)
            with patch.object(session_renew, "COOKIE_DIR", cookie_dir), patch.object(
                session_renew, "check_cookie", fake_check_cookie
            ), patch.object(session_renew, "_mark_invalid", marked.append):
                row = session_renew.renew_account(self.ACCOUNT)

        self.assertEqual(row["status"], session_renew.STATUS_INVALID)
        self.assertEqual(marked, [7], "失效的账号要在账号列表里显示成异常")

    def test_busy_account_is_skipped_instead_of_queued(self):
        """这个账号正在发布时，续期要**让路**而不是排队等（wait_timeout=0）。"""
        seen = {}

        @contextmanager
        def fake_session(key, on_wait=None, cancel_event=None, wait_timeout=None):
            seen["wait_timeout"] = wait_timeout
            raise BrowserLockTimeout("账号忙")
            yield  # pragma: no cover

        with tempfile.TemporaryDirectory() as tmp:
            cookie_dir = self._with_cookie_file(tmp)
            with patch.object(session_renew, "COOKIE_DIR", cookie_dir), patch.object(
                session_renew, "browser_session", fake_session
            ):
                row = session_renew.renew_account(self.ACCOUNT)

        self.assertEqual(row["status"], session_renew.STATUS_BUSY)
        self.assertEqual(seen["wait_timeout"], 0)

    def test_unexpected_error_does_not_escape(self):
        async def boom(*_args, **_kwargs):
            raise RuntimeError("页面打不开")

        with tempfile.TemporaryDirectory() as tmp:
            cookie_dir = self._with_cookie_file(tmp)
            with patch.object(session_renew, "COOKIE_DIR", cookie_dir), patch.object(
                session_renew, "check_cookie", boom
            ):
                row = session_renew.renew_account(self.ACCOUNT)

        self.assertEqual(row["status"], session_renew.STATUS_ERROR)
        self.assertIn("页面打不开", row["message"])


class RunOnceTest(unittest.TestCase):
    def setUp(self):
        session_renew._pending.clear()
        session_renew._done_slots.clear()
        session_renew._running = False
        session_renew._last_run = None

    def test_run_once_summarises_every_account(self):
        accounts = [
            {"id": 1, "type": 1, "file_path": "a.json", "name": "A", "platform": "小红书"},
            {"id": 2, "type": 4, "file_path": "b.json", "name": "B", "platform": "快手"},
            {"id": 3, "type": 2, "file_path": "c.json", "name": "C", "platform": "视频号"},
        ]
        results = {
            1: {"status": session_renew.STATUS_OK, "message": "ok"},
            2: {"status": session_renew.STATUS_INVALID, "message": "失效"},
            3: {"status": session_renew.STATUS_BUSY, "message": "忙"},
        }

        def fake_renew(account):
            return {"id": account["id"], "name": account["name"], "platform": account["platform"],
                    **results[account["id"]]}

        with patch.object(session_renew, "list_accounts", lambda: accounts), patch.object(
            session_renew, "renew_account", fake_renew
        ):
            run = session_renew.run_once("定时 07:00")

        self.assertEqual((run["total"], run["ok"], run["invalid"], run["busy"], run["error"]), (3, 1, 1, 1, 0))
        self.assertEqual(len(run["accounts"]), 3)

    def test_only_ids_limits_the_run(self):
        accounts = [
            {"id": 1, "type": 1, "file_path": "a.json", "name": "A", "platform": "小红书"},
            {"id": 2, "type": 4, "file_path": "b.json", "name": "B", "platform": "快手"},
        ]
        renewed = []

        def fake_renew(account):
            renewed.append(account["id"])
            return {
                "id": account["id"],
                "name": account["name"],
                "platform": account["platform"],
                "status": session_renew.STATUS_OK,
                "message": "ok",
            }

        with patch.object(session_renew, "list_accounts", lambda: accounts), patch.object(
            session_renew, "renew_account", fake_renew
        ):
            session_renew.run_once("补跑", only_ids=[2])

        self.assertEqual(renewed, [2], "补跑只该跑被占用的那个账号")

    def test_busy_accounts_are_retried_a_bounded_number_of_times(self):
        run = {
            "accounts": [{"id": 5, "status": session_renew.STATUS_BUSY}],
            "total": 1, "ok": 0, "invalid": 0, "busy": 1, "error": 0,
        }
        for _ in range(session_renew.BUSY_RETRY_LIMIT):
            session_renew._remember_run(run)
        self.assertIn(5, session_renew._pending, "被占用的账号要排一次补跑")

        session_renew._remember_run(run)  # 第 LIMIT+1 次：放弃，别一直重试
        self.assertNotIn(5, session_renew._pending)

    def test_success_clears_the_retry_queue(self):
        session_renew._remember_run({"accounts": [{"id": 5, "status": session_renew.STATUS_BUSY}]})
        self.assertIn(5, session_renew._pending)
        session_renew._remember_run({"accounts": [{"id": 5, "status": session_renew.STATUS_OK}]})
        self.assertNotIn(5, session_renew._pending)


class TickTest(unittest.TestCase):
    def setUp(self):
        session_renew._pending.clear()
        session_renew._done_slots.clear()
        session_renew._running = False
        session_renew._last_run = None

    def _config(self, enabled=True, times=("00:00", "07:00")):
        return {"enabled": enabled, "times": list(times)}

    def test_tick_starts_the_run_at_the_configured_minute(self):
        started = []
        with patch.object(session_renew, "get_session_renew_config", lambda: self._config()), patch.object(
            session_renew, "_start_run", lambda reason, ids=None: started.append(reason) or True
        ):
            outcome = session_renew.tick(datetime(2026, 9, 30, 7, 0, 3))

        self.assertEqual(outcome["slots"], ["07:00"])
        self.assertEqual(started, ["定时 07:00"])

    def test_second_tick_in_the_same_minute_does_not_run_again(self):
        started = []
        with patch.object(session_renew, "get_session_renew_config", lambda: self._config()), patch.object(
            session_renew, "_start_run", lambda reason, ids=None: started.append(reason) or True
        ):
            session_renew.tick(datetime(2026, 9, 30, 7, 0, 3))
            session_renew.tick(datetime(2026, 9, 30, 7, 0, 33))

        self.assertEqual(started, ["定时 07:00"], "同一分钟里第二次 tick 不能再起一轮")

    def test_tick_does_nothing_when_disabled(self):
        started = []
        with patch.object(
            session_renew, "get_session_renew_config", lambda: self._config(enabled=False)
        ), patch.object(session_renew, "_start_run", lambda reason, ids=None: started.append(reason) or True):
            outcome = session_renew.tick(datetime(2026, 9, 30, 7, 0, 3))

        self.assertEqual(started, [])
        self.assertEqual(outcome["slots"], [])

    def test_done_slots_are_cleaned_up_across_days(self):
        """长期运行后 _done_slots 不能无限长大。"""
        with patch.object(session_renew, "get_session_renew_config", lambda: self._config()), patch.object(
            session_renew, "_start_run", lambda reason, ids=None: True
        ):
            session_renew.tick(datetime(2026, 9, 30, 7, 0, 3))
            self.assertIn(("2026-09-30", "07:00"), session_renew._done_slots)
            session_renew.tick(datetime(2026, 10, 1, 7, 0, 3))

        self.assertNotIn(("2026-09-30", "07:00"), session_renew._done_slots)
        self.assertIn(("2026-10-01", "07:00"), session_renew._done_slots)

    def test_due_retry_is_started_with_just_those_accounts(self):
        session_renew._pending[9] = {"attempts": 1, "due_at": 0.0}
        started = []
        with patch.object(session_renew, "get_session_renew_config", lambda: self._config()), patch.object(
            session_renew, "_start_run", lambda reason, ids=None: started.append((reason, list(ids or []))) or True
        ):
            session_renew.tick(datetime(2026, 9, 30, 9, 0, 0))

        self.assertEqual(len(started), 1)
        self.assertEqual(started[0][1], [9])


class StatusTest(unittest.TestCase):
    def test_status_reports_the_next_slot(self):
        with patch.object(
            session_renew, "get_session_renew_config",
            lambda: {"enabled": True, "times": ["00:00", "07:00", "14:00", "20:00"]},
        ):
            state = session_renew.status()
        self.assertTrue(state["enabled"])
        self.assertEqual(len(state["times"]), 4)
        self.assertIsNotNone(state["next_run"])

    def test_status_has_no_next_run_when_disabled(self):
        with patch.object(
            session_renew, "get_session_renew_config", lambda: {"enabled": False, "times": ["07:00"]}
        ):
            self.assertIsNone(session_renew.status()["next_run"])


class NonBlockingSessionTest(unittest.TestCase):
    """`browser_session(wait_timeout=0)` = 账号忙就让路（续期靠它，不然会把发布堵住）。"""

    def test_zero_wait_raises_immediately_when_the_account_is_held(self):
        key = "unittest-session-renew-busy"
        lock = account_lock(key)
        holder = threading.Event()
        release = threading.Event()

        def hold():
            lock.acquire()
            holder.set()
            release.wait(5)
            lock.release()

        thread = threading.Thread(target=hold, daemon=True)
        thread.start()
        try:
            holder.wait(5)
            with self.assertRaises(BrowserLockTimeout):
                with browser_session(key, wait_timeout=0):
                    pass  # pragma: no cover
        finally:
            release.set()
            thread.join(5)

    def test_session_is_reusable_after_a_zero_wait_failure(self):
        key = "unittest-session-renew-reuse"
        lock = account_lock(key)
        lock.acquire()
        lock.release()
        with browser_session(key, wait_timeout=0):
            pass  # 拿到就正常进出，锁和名额都要还回去


class RenewLoopAsyncioTest(unittest.TestCase):
    """`run_once` 是在工作线程里跑的，那边没有事件循环——必须自己 asyncio.run。"""

    def test_renew_account_runs_the_async_check(self):
        ran = []

        async def fake_check_cookie(*_args, **_kwargs):
            ran.append(asyncio.get_running_loop() is not None)
            return True

        account = {"id": 1, "type": 1, "file_path": "acc.json", "name": "A", "platform": "小红书"}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "acc.json").write_text("[]", encoding="utf-8")
            with patch.object(session_renew, "COOKIE_DIR", Path(tmp)), patch.object(
                session_renew, "check_cookie", fake_check_cookie
            ):
                row = session_renew.renew_account(account)

        self.assertEqual(row["status"], session_renew.STATUS_OK)
        self.assertEqual(ran, [True])


class SessionRenewApiTest(unittest.TestCase):
    """`/session-renew` 的读写。

    前端两个页面上的「定时续期」按钮都走这个接口（「账号管理」的工具栏 + 「设置」页），
    所以它的契约要钉住：只传 enabled 不动时间点、时间点会被归一化、全是废值时给 400
    而不是默默存一个空列表（那会让开关显示"开启"却一个点都不跑）。

    顺带钉住**路径**：它**不在** `/xhs` 前缀下。续期的对象是账号管理里的全部账号
    （四个平台），挂在小红书前缀下会让人以为只续小红书。
    """

    @classmethod
    def setUpClass(cls):
        from flask import Flask

        from sau_backend_session import session_bp

        app = Flask(__name__)
        app.register_blueprint(session_bp)
        cls.client = app.test_client()

    def test_routes_are_not_under_the_xhs_prefix(self):
        rules = {str(rule) for rule in self.client.application.url_map.iter_rules()}
        self.assertIn("/session-renew", rules)
        self.assertFalse([r for r in rules if r.startswith("/xhs")])

    def test_get_reports_switch_times_and_next_run(self):
        with _settings_in_tmpdir():
            body = self.client.get("/session-renew").get_json()
        self.assertEqual(body["code"], 200)
        self.assertTrue(body["data"]["enabled"])
        self.assertEqual(body["data"]["times"], ["00:00", "07:00", "14:00", "20:00"])
        self.assertIn("next_run_time", body["data"])

    def test_toggle_only_touches_the_switch(self):
        with _settings_in_tmpdir():
            body = self.client.post("/session-renew", json={"enabled": False}).get_json()
        self.assertEqual(body["code"], 200)
        self.assertFalse(body["data"]["enabled"])
        self.assertEqual(body["data"]["times"], ["00:00", "07:00", "14:00", "20:00"])

    def test_times_are_normalized_and_switch_untouched(self):
        with _settings_in_tmpdir():
            self.client.post("/session-renew", json={"enabled": False})
            body = self.client.post("/session-renew", json={"times": ["7:00", "21:15", "7:00"]}).get_json()
        self.assertEqual(body["data"]["times"], ["07:00", "21:15"])
        self.assertFalse(body["data"]["enabled"], "改时间点不该顺手把开关打开")

    def test_only_invalid_times_is_rejected(self):
        with _settings_in_tmpdir():
            response = self.client.post("/session-renew", json={"times": ["乱写", "0700"]})
            self.assertEqual(response.status_code, 400)
            self.assertIn("至少需要一个有效的时间点", response.get_json()["msg"])
            # 而且不能把配置改坏
            self.assertEqual(
                app_settings.get_session_renew_config()["times"],
                ["00:00", "07:00", "14:00", "20:00"],
            )

    def test_empty_body_is_rejected(self):
        with _settings_in_tmpdir():
            self.assertEqual(self.client.post("/session-renew", json={}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
