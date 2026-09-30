"""后台登录认证（myUtils/web_auth.py）。

这是全站的准入闸门，测的重点是三件事：
  1. **密码只生成一次**：文件里已有密码时绝不覆盖，否则每次重启都会把人的密码换掉；
  2. **挡不挡得住**：未登录时业务接口必须 401，而页面外壳和静态资源必须放行
     （不放行的话登录页自己都加载不出来）；
  3. **不泄漏**：`auth` 段（明文密码）既不能从模型设置接口读出去，也不能写进去。

测试全程把 `SETTINGS_PATH` 指到临时文件，**不碰仓库里那份真的 settings.json**
（那里面有真的 API Key 和真的登录密码）。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from myUtils import web_auth


class _AuthTestCase(unittest.TestCase):
    """公共脚手架：每个测试一份独立的临时 settings.json。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = Path(self._tmp.name) / "settings.json"
        # 预置一份"真实"配置：有模型设置、没有 auth 段
        self.settings.write_text(
            json.dumps({"vision": {"base_url": "https://example.com/v1", "api_key": "sk-secret"}}),
            encoding="utf-8",
        )

        self._patches = [
            patch.object(web_auth, "SETTINGS_PATH", self.settings),
            # 密钥是按 mtime 缓存的，测试里文件换来换去，必须清掉
            patch.object(web_auth, "_secret_cache", None),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

        self.addCleanup(self._tmp.cleanup)
        web_auth.clear_failures("127.0.0.1")

    def read_settings(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8"))


class TestPasswordBootstrap(_AuthTestCase):
    def test_first_run_generates_password_and_persists(self):
        password, generated = web_auth.ensure_auth()

        self.assertTrue(generated, "首次运行应该生成新密码")
        self.assertEqual(len(password), web_auth.PASSWORD_LENGTH)
        self.assertEqual(self.read_settings()["auth"]["password"], password)
        self.assertEqual(self.read_settings()["auth"]["username"], "admin")

    def test_existing_password_is_never_overwritten(self):
        """核心不变量：第二次、第三次启动都必须沿用同一个密码。"""
        first, gen1 = web_auth.ensure_auth()
        second, gen2 = web_auth.ensure_auth()

        self.assertTrue(gen1)
        self.assertFalse(gen2, "已经有密码了就不该再生成")
        self.assertEqual(first, second)

    def test_bootstrap_keeps_other_settings_intact(self):
        web_auth.ensure_auth()
        data = self.read_settings()

        self.assertEqual(data["vision"]["api_key"], "sk-secret", "模型设置不能被启动流程动到")
        self.assertIn("secret_key", data["auth"])
        self.assertTrue(data["auth"]["secret_key"])

    def test_does_not_rewrite_file_when_nothing_to_do(self):
        web_auth.ensure_auth()
        before = self.settings.read_text(encoding="utf-8")

        web_auth.ensure_auth()

        self.assertEqual(before, self.settings.read_text(encoding="utf-8"), "没缺东西就不该重写文件")

    def test_empty_password_is_regenerated(self):
        """手工把密码清空 = 要求换一个新密码。"""
        first, _ = web_auth.ensure_auth()
        data = self.read_settings()
        data["auth"]["password"] = ""
        self.settings.write_text(json.dumps(data), encoding="utf-8")

        second, generated = web_auth.ensure_auth()

        self.assertTrue(generated)
        self.assertNotEqual(first, second)

    def test_password_avoids_lookalike_characters(self):
        """密码要人从文件里抄到登录框，0/O、1/l/I 这类必须排除。"""
        password, _ = web_auth.ensure_auth()
        for ch in "0Oo1lI":
            self.assertNotIn(ch, password, f"密码里不该出现易混字符 {ch}")


class TestVerifyCredentials(_AuthTestCase):
    def setUp(self):
        super().setUp()
        self.password, _ = web_auth.ensure_auth()

    def test_correct_credentials(self):
        self.assertTrue(web_auth.verify_credentials("admin", self.password))

    def test_wrong_password(self):
        self.assertFalse(web_auth.verify_credentials("admin", "nope"))

    def test_wrong_username(self):
        self.assertFalse(web_auth.verify_credentials("root", self.password))

    def test_empty_password_input(self):
        self.assertFalse(web_auth.verify_credentials("admin", ""))

    def test_fails_closed_when_password_missing(self):
        """文件被写坏（没有 auth 段）时应该拒绝登录，而不是放行。"""
        data = self.read_settings()
        data.pop("auth")
        self.settings.write_text(json.dumps(data), encoding="utf-8")
        web_auth._secret_cache = None

        self.assertFalse(web_auth.verify_credentials("admin", ""))
        self.assertFalse(web_auth.verify_credentials("admin", self.password))


class TestToken(_AuthTestCase):
    def setUp(self):
        super().setUp()
        web_auth.ensure_auth()

    def test_round_trip(self):
        token = web_auth.issue_token()
        self.assertEqual(web_auth.verify_token(token), "admin")

    def test_tampered_token_rejected(self):
        token = web_auth.issue_token()
        self.assertIsNone(web_auth.verify_token(token[:-3] + "abc"))

    def test_garbage_rejected(self):
        for bad in ("", "abc", "a.b.c", "Bearer xxx"):
            self.assertIsNone(web_auth.verify_token(bad), f"{bad!r} 不该通过")

    def test_token_signed_by_another_secret_rejected(self):
        """换掉 secret_key = 让所有已发出的 token 立刻失效。"""
        token = web_auth.issue_token()
        data = self.read_settings()
        data["auth"]["secret_key"] = web_auth._new_secret_key()
        self.settings.write_text(json.dumps(data), encoding="utf-8")
        web_auth._secret_cache = None

        self.assertIsNone(web_auth.verify_token(token))

    def test_expired_token_rejected(self):
        token = web_auth.issue_token()
        with patch.object(web_auth, "TOKEN_MAX_AGE", -1):
            self.assertIsNone(web_auth.verify_token(token))


class TestTokenFromRequest(_AuthTestCase):
    def test_bearer_header(self):
        self.assertEqual(web_auth.token_from({"Authorization": "Bearer abc"}, {}), "abc")

    def test_query_string_for_sse(self):
        """EventSource 设不了请求头，只能走查询串。"""
        self.assertEqual(web_auth.token_from({}, {"token": "abc"}), "abc")

    def test_header_wins_over_query(self):
        self.assertEqual(web_auth.token_from({"Authorization": "Bearer h"}, {"token": "q"}), "h")

    def test_empty(self):
        self.assertEqual(web_auth.token_from({}, {}), "")


class TestLockout(_AuthTestCase):
    def test_locks_after_repeated_failures(self):
        client = "10.0.0.9"
        self.assertEqual(web_auth.lockout_remaining(client), 0.0)

        for _ in range(web_auth.LOCKOUT_THRESHOLD):
            web_auth.record_failure(client)

        self.assertGreater(web_auth.lockout_remaining(client), 0)

    def test_success_clears_counter(self):
        client = "10.0.0.9"
        for _ in range(web_auth.LOCKOUT_THRESHOLD - 1):
            web_auth.record_failure(client)

        web_auth.clear_failures(client)

        self.assertEqual(web_auth.lockout_remaining(client), 0.0)


class TestSettingsApiDoesNotLeakAuth(_AuthTestCase):
    """`auth` 段是明文密码，绝不能被模型设置接口带出去或者改掉。"""

    def setUp(self):
        super().setUp()
        self.password, _ = web_auth.ensure_auth()

    def test_public_settings_hides_auth(self):
        from myUtils import app_settings

        with patch.object(app_settings, "SETTINGS_PATH", self.settings):
            public = app_settings.public_settings()

        self.assertNotIn("auth", public)
        self.assertNotIn(self.password, json.dumps(public, ensure_ascii=False))

    def test_save_settings_cannot_overwrite_password(self):
        from myUtils import app_settings

        with patch.object(app_settings, "SETTINGS_PATH", self.settings):
            app_settings.save_settings({"auth": {"password": "hacked"}})

        self.assertEqual(self.read_settings()["auth"]["password"], self.password)
        # 顺带确认正常字段还能存
        with patch.object(app_settings, "SETTINGS_PATH", self.settings):
            app_settings.save_settings({"default_collection": "美食"})
        self.assertEqual(self.read_settings()["default_collection"], "美食")


class TestGuard(unittest.TestCase):
    """`install()` 装在真 Flask app 上的行为。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = Path(self._tmp.name) / "settings.json"
        self.settings.write_text("{}", encoding="utf-8")

        self._patches = [
            patch.object(web_auth, "SETTINGS_PATH", self.settings),
            patch.object(web_auth, "_secret_cache", None),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

        app = Flask(__name__)

        @app.route("/")
        def index():  # noqa: ANN202
            return "shell"

        @app.route("/assets/<filename>")
        def custom_static(filename):  # noqa: ANN001, ANN202
            return f"asset:{filename}"

        @app.route("/secret-data")
        def secret_data():  # noqa: ANN202
            return "business data"

        web_auth.install(app, verbose=False)
        self.app = app
        self.client = app.test_client()
        self.password = web_auth.get_password()
        web_auth.clear_failures("127.0.0.1")

    def token(self) -> str:
        resp = self.client.post(
            "/auth/login", json={"username": "admin", "password": self.password}
        )
        return resp.get_json()["data"]["token"]

    def test_business_route_requires_login(self):
        self.assertEqual(self.client.get("/secret-data").status_code, 401)

    def test_public_routes_stay_open(self):
        """页面外壳和静态资源必须公开，否则登录页加载不出来。"""
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/assets/app.js").status_code, 200)

    def test_login_then_access(self):
        token = self.token()
        resp = self.client.get("/secret-data", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(as_text=True), "business data")

    def test_query_string_token_works(self):
        """SSE（EventSource）只能靠查询串带 token。"""
        token = self.token()
        self.assertEqual(self.client.get(f"/secret-data?token={token}").status_code, 200)

    def test_wrong_password_gets_401_not_token(self):
        resp = self.client.post(
            "/auth/login", json={"username": "admin", "password": "wrong"}
        )
        self.assertEqual(resp.status_code, 401)
        self.assertIsNone(resp.get_json()["data"])

    def test_options_preflight_is_not_blocked(self):
        """CORS 预检不带 Authorization，挡掉会让跨域请求全废。"""
        resp = self.client.options("/secret-data")
        self.assertNotEqual(resp.status_code, 401)

    def test_status_endpoint_reports_state(self):
        self.assertFalse(self.client.get("/auth/status").get_json()["data"]["authenticated"])

        token = self.token()
        data = self.client.get(
            "/auth/status", headers={"Authorization": f"Bearer {token}"}
        ).get_json()["data"]
        self.assertTrue(data["authenticated"])
        self.assertEqual(data["username"], "admin")


class TestSettingsPathIsRenamed(unittest.TestCase):
    def test_settings_path_points_at_settings_json(self):
        from myUtils.app_settings import SETTINGS_PATH

        self.assertEqual(Path(SETTINGS_PATH).name, "settings.json")


if __name__ == "__main__":
    unittest.main()
