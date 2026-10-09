# -*- coding: utf-8 -*-
"""头像落地 + 本地头像路由（at-ai-xhs 改造新增）。

用户诉求的原话：「同步过账号详情的账号把头像也同步到本地来，不然本地的几个头像还是显示的文字」。
所以这里要钉住的是"同步时确实把图存下来了"和"存过之后接口能把它发出去"，
以及两个容易做坏的细节：

1. **别每次同步都重下一遍**（同步有三个入口：详情页、定时续期、新账号登录）——
   来源 URL 没变就复用本地那张；
2. **头像失败不能影响同步结论**——没有头像只是列表少张图，粉丝/作品那些主数据照旧。
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

from flask import Flask

from myUtils.account_stats import avatar


class _FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200, content_type: str = "image/jpeg"):
        self.content = content
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}


class StoreAvatarTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.avatar_dir = Path(self._tmp.name) / "avatars"
        self._patch = patch.object(avatar, "AVATAR_DIR", self.avatar_dir)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_downloads_and_records_source_url(self):
        with patch("requests.get", return_value=_FakeResponse(b"\xff\xd8image-bytes")) as fake:
            name = avatar.store_avatar(7, "https://avatar3.pddpic.com/a/xxx.jpg")

        self.assertEqual(name, "7.jpg")
        self.assertEqual((self.avatar_dir / "7.jpg").read_bytes(), b"\xff\xd8image-bytes")
        # 记下来源 URL，供"下次别重复下载"用
        self.assertEqual((self.avatar_dir / "7.url").read_text(encoding="utf-8"), "https://avatar3.pddpic.com/a/xxx.jpg")
        self.assertEqual(fake.call_count, 1)

    def test_same_url_does_not_download_twice(self):
        """同步有三个入口，同一个 URL 不该反复下载。"""
        with patch("requests.get", return_value=_FakeResponse(b"abc")) as fake:
            avatar.store_avatar(7, "https://cdn/a.jpg")
            avatar.store_avatar(7, "https://cdn/a.jpg")
            self.assertEqual(fake.call_count, 1, "同一个头像 URL 被重复下载了")

    def test_changed_url_downloads_again(self):
        with patch("requests.get", return_value=_FakeResponse(b"abc")) as fake:
            avatar.store_avatar(7, "https://cdn/old.jpg")
            avatar.store_avatar(7, "https://cdn/new.jpg")
            self.assertEqual(fake.call_count, 2, "换了头像 URL 却没重新下载")

    def test_extension_follows_content_type(self):
        with patch("requests.get", return_value=_FakeResponse(b"png-bytes", content_type="image/png")):
            self.assertEqual(avatar.store_avatar(7, "https://cdn/a"), "7.png")

    def test_http_error_is_not_fatal(self):
        with patch("requests.get", return_value=_FakeResponse(b"", status_code=403)):
            self.assertIsNone(avatar.store_avatar(7, "https://cdn/a.jpg"))
        self.assertIsNone(avatar.avatar_file(7))

    def test_oversized_image_is_rejected(self):
        big = b"x" * (avatar._MAX_BYTES + 1)
        with patch("requests.get", return_value=_FakeResponse(big)):
            self.assertIsNone(avatar.store_avatar(7, "https://cdn/a.jpg"))

    def test_missing_or_odd_urls_are_skipped_without_request(self):
        with patch("requests.get") as fake:
            self.assertIsNone(avatar.store_avatar(7, None))
            self.assertIsNone(avatar.store_avatar(7, ""))
            self.assertIsNone(avatar.store_avatar(7, "not-a-url"))
            self.assertIsNone(avatar.store_avatar(0, "https://cdn/a.jpg"))
            fake.assert_not_called()

    def test_network_exception_does_not_raise(self):
        with patch("requests.get", side_effect=RuntimeError("网络断了")):
            self.assertIsNone(avatar.store_avatar(7, "https://cdn/a.jpg"))

    def test_avatar_file_finds_existing_image(self):
        self.avatar_dir.mkdir(parents=True, exist_ok=True)
        (self.avatar_dir / "9.png").write_bytes(b"png")
        (self.avatar_dir / "9.url").write_text("https://cdn/a.png", encoding="utf-8")
        found = avatar.avatar_file(9)
        self.assertIsNotNone(found)
        self.assertEqual(found.suffix, ".png")
        self.assertIsNone(avatar.avatar_file(123))


class SyncStoresAvatarTests(unittest.TestCase):
    """同步流程里要**真的**去落头像，而且失败不能改写同步结论。"""

    def _stats(self):
        return {
            "account": {"name": "麻辣腐竹", "avatar": "https://avatar3.pddpic.com/a/xxx.jpg"},
            "fans": {"count": 1},
            "media": {"supported": True, "items": [{"id": "1"}]},
        }

    def test_sync_calls_store_avatar(self):
        from myUtils.account_stats import sync

        account = {"id": 7, "type": 5, "file_path": "pdd.json", "name": "麻辣腐竹", "platform": "拼多多视频"}
        with patch("myUtils.account_stats.fetch", return_value=self._stats()), patch(
            "myUtils.account_stats.cache.save"
        ), patch("myUtils.account_stats.avatar.store_avatar", return_value="7.jpg") as store:
            row = sync.sync_account(account, with_media=False)

        store.assert_called_once_with(7, "https://avatar3.pddpic.com/a/xxx.jpg")
        self.assertEqual(row["status"], "ok")
        self.assertIn("头像已更新", row["message"])

    def test_avatar_failure_does_not_break_sync(self):
        """头像下载炸了也只是少张图——同步结论必须还是 ok。"""
        from myUtils.account_stats import sync

        account = {"id": 7, "type": 5, "file_path": "pdd.json", "name": "麻辣腐竹", "platform": "拼多多视频"}
        with patch("myUtils.account_stats.fetch", return_value=self._stats()), patch(
            "myUtils.account_stats.cache.save"
        ), patch("myUtils.account_stats.avatar.store_avatar", side_effect=RuntimeError("炸了")):
            row = sync.sync_account(account, with_media=False)

        self.assertEqual(row["status"], "ok")
        self.assertNotIn("头像", row["message"])


class AvatarRouteTests(unittest.TestCase):
    """本地头像路由：有图就发图，没有就 404（前端据此回退文字头像）。"""

    def setUp(self):
        from sau_backend_stats import account_stats_bp

        self._tmp = tempfile.TemporaryDirectory()
        self.avatar_dir = Path(self._tmp.name) / "avatars"
        self._patch = patch.object(avatar, "AVATAR_DIR", self.avatar_dir)
        self._patch.start()
        app = Flask(__name__)
        app.register_blueprint(account_stats_bp)
        self.client = app.test_client()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_serves_local_avatar(self):
        self.avatar_dir.mkdir(parents=True, exist_ok=True)
        (self.avatar_dir / "7.jpg").write_bytes(b"\xff\xd8fake-jpeg")
        response = self.client.get("/account-stats/avatar/7")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"\xff\xd8fake-jpeg")
        self.assertIn("image", response.headers.get("Content-Type", ""))

    def test_404_when_missing(self):
        response = self.client.get("/account-stats/avatar/42")
        self.assertEqual(response.status_code, 404)
        payload = response.get_json()
        self.assertFalse(payload["success"])
        self.assertIn("同步", payload["msg"])


if __name__ == "__main__":
    unittest.main()
