# -*- coding: utf-8 -*-
"""背景音乐：上传 → 合成 → 改名落库（`sau_backend_music.py` / `myUtils/background_music.py`）。

这个功能的验收点其实就四条，测试盯着它们：

1. **原音轨真的没了**——不是"加了音乐"，是**换**了音轨。所以这里不看文件名、不看返回值，
   而是把成品抽成 wav 做一次 FFT：原视频里是 200Hz、背景音乐是 2000Hz，
   成品必须是 2000Hz。只看"有没有音频轨"是验证不了这件事的。
2. **音乐比视频短就循环**——1 秒的音乐 + 3 秒的视频，成品必须还是 3 秒。
3. **音乐比视频长就截断**——5 秒的音乐 + 3 秒的视频，成品同样是 3 秒。
4. **文件名带上音乐名**——需求里"一眼看出用的哪首音乐"就靠它；而且反复换音乐时
   名字不能越滚越长（`标题-音乐A-音乐B-音乐C.mp4`）。

全程把 `BASE_DIR` / `BGM_DIR` 指到临时目录：**不碰仓库里真实的 videoFile、bgmFile 和
database.db**（那里面有用户自己的素材和记录）。
"""

import io
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import sau_backend_music as music_backend
import sau_backend_xhs
from myUtils import background_music as bgm

try:  # numpy 是 opencv-python 的依赖，正常都在；真缺了就少验一条频率断言
    import numpy as np
except Exception:  # pragma: no cover
    np = None

#: 原视频自带的音频频率（低频）
VIDEO_TONE = 200
#: 背景音乐 A 的频率（高频）
MUSIC_A_TONE = 2000
#: 背景音乐 B 的频率
MUSIC_B_TONE = 3000

SCHEMA = """
CREATE TABLE IF NOT EXISTS file_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    filesize REAL,
    upload_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    file_path TEXT
);
CREATE TABLE IF NOT EXISTS material_copy (
    file_path TEXT PRIMARY KEY,
    title TEXT,
    desc TEXT,
    tags TEXT,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def _ffmpeg():
    return bgm.find_ffmpeg()


@unittest.skipUnless(_ffmpeg(), "需要 ffmpeg（requirements.txt 里的 imageio-ffmpeg 自带）")
class _MusicTestCase(unittest.TestCase):
    """公共脚手架：一个临时 BASE_DIR（videoFile / bgmFile / db），外加一条 3 秒测试素材。"""

    ffmpeg = ""

    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = _ffmpeg()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        (cls.root / "videoFile").mkdir(parents=True, exist_ok=True)
        (cls.root / "bgmFile").mkdir(parents=True, exist_ok=True)
        (cls.root / "db").mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(cls.root / "db" / "database.db") as conn:
            conn.executescript(SCHEMA)

        cls.material = "测试素材.mp4"
        cls._render_video(cls.root / "videoFile" / cls.material, 3, VIDEO_TONE, "aac")
        cls._render_audio(cls.root / "short.mp3", 1, MUSIC_A_TONE)
        cls._render_audio(cls.root / "long.mp3", 5, MUSIC_B_TONE)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    # ---- 造素材 -------------------------------------------------------------

    @classmethod
    def _run(cls, args):
        proc = subprocess.run(
            [cls.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args],
            capture_output=True,
        )
        if proc.returncode != 0:
            raise AssertionError(proc.stderr.decode("utf-8", "ignore"))

    @classmethod
    def _render_video(cls, path, seconds, tone, audio_codec="aac", video_codec="libx264", extra=()):
        cls._run(
            [
                "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=15:duration={seconds}",
                "-f", "lavfi", "-i", f"sine=frequency={tone}:duration={seconds}",
                "-c:v", video_codec, *extra, "-pix_fmt", "yuv420p",
                "-c:a", audio_codec, "-shortest", str(path),
            ]
        )

    @classmethod
    def _render_audio(cls, path, seconds, tone):
        cls._run(
            ["-f", "lavfi", "-i", f"sine=frequency={tone}:duration={seconds}",
             "-c:a", "libmp3lame", str(path)]
        )

    # ---- 每个用例都从干净状态开始 -------------------------------------------

    def setUp(self):
        video_dir = self.root / "videoFile"
        for f in video_dir.iterdir():
            f.unlink()
        self._render_video(video_dir / self.material, 3, VIDEO_TONE)

        self._patches = [
            patch.object(bgm, "BASE_DIR", self.root),
            patch.object(bgm, "BGM_DIR", self.root / "bgmFile"),
            patch.object(music_backend, "BASE_DIR", self.root),
            # 合成后会调 sau_backend_xhs 的两个函数来同步 file_records / AI 文案，
            # 它们各自按自己模块里的 BASE_DIR 找库和素材目录，也要一起指过来
            patch.object(sau_backend_xhs, "BASE_DIR", self.root),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

        # 背景音乐那两张表是后端按需建的（`ensure_tables`），这里走一次真实的建表路径，
        # 顺便保证下面的 DELETE 一定有的删。
        bgm.ensure_music_tables()
        with sqlite3.connect(self.root / "db" / "database.db") as conn:
            conn.execute("DELETE FROM file_records")
            conn.execute("DELETE FROM material_copy")
            conn.execute("DELETE FROM background_music")
            conn.execute("DELETE FROM material_bgm")
            conn.execute(
                "INSERT INTO file_records (filename, filesize, file_path) VALUES (?, ?, ?)",
                (self.material, 1.0, self.material),
            )
            conn.commit()

        self.app = Flask(__name__)
        self.app.register_blueprint(music_backend.music_bp)
        self.client = self.app.test_client()

        with music_backend._tasks_lock:
            music_backend._tasks.clear()
        music_backend._cancel_flags.clear()

    # ---- 调用接口的小工具 ---------------------------------------------------

    def upload_music(self, path: Path, name: str) -> int:
        res = self.client.post(
            "/music/upload",
            data={"file": (io.BytesIO(path.read_bytes()), path.name), "name": name},
            content_type="multipart/form-data",
        )
        body = res.get_json()
        self.assertEqual(body["code"], 200, body)
        return body["data"]["id"]

    def compose_and_wait(self, material: str, music_id: int, timeout: float = 60.0) -> dict:
        res = self.client.post("/music/compose", json={"material": material, "music_id": music_id})
        body = res.get_json()
        self.assertEqual(body["code"], 200, body)
        task_id = body["data"]["task_id"]

        deadline = time.time() + timeout
        while time.time() < deadline:
            snap = self.client.get(f"/music/task/{task_id}").get_json()["data"]
            if snap["state"] in ("done", "failed", "cancelled"):
                return snap
            time.sleep(0.1)
        self.fail("合成任务在超时前没有结束")

    def file_record(self, file_path: str) -> dict | None:
        with sqlite3.connect(self.root / "db" / "database.db") as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM file_records WHERE file_path = ?", (file_path,)
            ).fetchone()
            return dict(row) if row else None

    def dominant_frequency(self, media: Path) -> int:
        """把媒体的音轨抽成 wav，用 FFT 找出主频——用来证明"换了音轨"而不是"混了音轨"。"""
        out = self.root / "probe.wav"
        if out.exists():
            out.unlink()
        self._run(["-i", str(media), "-vn", "-ac", "1", "-ar", "8000", "-t", "2", str(out)])
        with wave.open(str(out)) as w:
            data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(float)
            rate = w.getframerate()
        spectrum = np.abs(np.fft.rfft(data * np.hanning(len(data))))
        return int(rate * int(spectrum.argmax()) / len(data))


class ComposeFunctionTest(_MusicTestCase):
    """`compose_video_with_music` 自己的收尾行为：失败/中断都不能留下半成品文件。"""

    def test_cancel_removes_the_partial_output(self):
        """点「中断合成」后，videoFile/ 里不能留下一个写了一半的 mp4——
        它会以隐藏临时文件的形式存在，用户看不见却占着几百 MB。"""
        flag = threading.Event()
        flag.set()  # 提交即已请求中断
        out = self.root / "videoFile" / f".bgm_tmp_{'cancel'}.mp4"

        ok, err = bgm.compose_video_with_music(
            self.root / "videoFile" / self.material,
            self.root / "short.mp3",
            out,
            duration=3.0,
            cancel_event=flag,
        )

        self.assertFalse(ok)
        self.assertIn("取消", err)
        self.assertFalse(out.exists())

    def test_missing_music_fails_cleanly(self):
        out = self.root / "videoFile" / "should-not-exist.mp4"
        ok, err = bgm.compose_video_with_music(
            self.root / "videoFile" / self.material, self.root / "没有这首.mp3", out, duration=3.0
        )
        self.assertFalse(ok)
        self.assertIn("背景音乐", err)
        self.assertFalse(out.exists())

    def test_probe_reads_duration_and_tracks(self):
        info = bgm.probe_media(self.root / "videoFile" / self.material)
        self.assertAlmostEqual(info["duration"], 3.0, delta=0.2)
        self.assertTrue(info["has_video"])
        self.assertTrue(info["has_audio"])
        # 音乐文件：有音频、没有视频轨（上传时要靠这个把非音频文件挡掉）
        music = bgm.probe_media(self.root / "short.mp3")
        self.assertTrue(music["has_audio"])
        self.assertFalse(music["has_video"])


class NamingTest(_MusicTestCase):
    """文件名规则：`原名-音乐名.mp4`，且反复换音乐不会越滚越长。"""

    def test_appends_music_name(self):
        self.assertEqual(
            bgm.build_output_name("海边.mp4", "轻音乐", ["轻音乐"]), "海边-轻音乐.mp4"
        )

    def test_replaces_previous_music_suffix(self):
        """已经合成过的素材再换一首，应该得到 `海边-另一首.mp4`，不是 `海边-轻音乐-另一首.mp4`。"""
        self.assertEqual(
            bgm.build_output_name("海边-轻音乐.mp4", "另一首", ["轻音乐", "另一首"]),
            "海边-另一首.mp4",
        )

    def test_same_music_is_idempotent(self):
        """用同一首重合成时算出的名字要和当前文件名相同（才能原地覆盖，不留第二份）。"""
        self.assertEqual(
            bgm.build_output_name("海边-轻音乐.mp4", "轻音乐", ["轻音乐"]), "海边-轻音乐.mp4"
        )

    def test_unknown_hyphen_is_not_stripped(self):
        """标题里本来就有的 `-` 不能被误当成音乐后缀切掉。"""
        self.assertEqual(
            bgm.build_output_name("北京-上海.mp4", "轻音乐", ["轻音乐"]), "北京-上海-轻音乐.mp4"
        )

    def test_music_name_is_sanitized_for_filename(self):
        """音乐名里的 `/` 之类不能带进文件名（否则会写到别的目录去）。"""
        self.assertEqual(bgm.safe_music_segment("a/b:c"), "abc")
        self.assertEqual(bgm.build_output_name("海边.mp4", "a/b", []), "海边-ab.mp4")

    def test_uuid_prefix_is_not_carried_into_the_name(self):
        """上传的素材磁盘名带 uuid 前缀，展示名不带——合成后用**展示名**命名。

        否则用户会看到 `a1b2c3-...._海边-轻音乐.mp4`：刚要求去掉 UUID 列，
        结果 uuid 又跑到文件名里了。
        """
        disk = "a1b2c3d4-0000-1111-2222-333344445555_海边.mp4"
        self.assertEqual(
            bgm.build_output_name("海边.mp4", "轻音乐", ["轻音乐"], ext=Path(disk).suffix),
            "海边-轻音乐.mp4",
        )

    def test_extension_wins_over_a_dotty_display_name(self):
        """展示名里带点也不能把扩展名判断带偏（`Path.stem` 在这里会切错）。"""
        self.assertEqual(
            bgm.build_output_name("v1.2 海边", "轻音乐", [], ext=".mp4"), "v1.2 海边-轻音乐.mp4"
        )


class StatusPayloadTest(_MusicTestCase):
    """状态列的三档文案：没有记录 = 待合成，失败也回落到待合成（原因走 message）。"""

    def test_missing_record_means_pending(self):
        payload = bgm.status_payload(None)
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["status_text"], "待合成")

    def test_status_texts(self):
        self.assertEqual(bgm.status_payload({"status": "running"})["status_text"], "合成中")
        self.assertEqual(bgm.status_payload({"status": "done"})["status_text"], "已合成")
        failed = bgm.status_payload({"status": "failed", "message": "ffmpeg 失败"})
        self.assertEqual(failed["status_text"], "待合成")
        self.assertEqual(failed["message"], "ffmpeg 失败")

    def test_unknown_status_falls_back_to_pending(self):
        self.assertEqual(bgm.status_payload({"status": "???"})["status"], "pending")


class StaleStatusTest(_MusicTestCase):
    """重启后的残留状态：不清掉的话那一列会永远转圈，而且重新合成会被 409 拦住。"""

    def test_stale_running_is_reset_on_startup(self):
        bgm.set_status(self.material, "running", music_id=1, music_name="轻音乐", progress=40)

        self.assertEqual(bgm.reset_stale_statuses(), 1)

        payload = bgm.get_status(self.material)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["status_text"], "待合成")
        self.assertEqual(payload["progress"], 0)
        self.assertIn("重启", payload["message"])

    def test_finished_rows_are_untouched(self):
        bgm.set_status(self.material, "done", music_name="轻音乐", progress=100)
        self.assertEqual(bgm.reset_stale_statuses(), 0)
        self.assertEqual(bgm.get_status(self.material)["status"], "done")


class AiRenameKeepsMusicSuffixTest(_MusicTestCase):
    """「生成文案」也会改素材名（改成标题）。它必须把 `-音乐名` 一起带上，
    否则一次生成就把"用的哪首音乐"抹掉、状态行也会变成孤儿。"""

    def test_ai_rename_keeps_the_music_suffix_and_moves_the_status(self):
        from sau_backend_xhs import _rename_material_to_title

        music_id = self.upload_music(self.root / "short.mp3", "轻音乐")
        snap = self.compose_and_wait(self.material, music_id)

        new_path, note = _rename_material_to_title(
            str(self.root / "videoFile" / snap["output"]), "海边风景"
        )

        self.assertEqual(Path(new_path).name, "海边风景-轻音乐.mp4", note)
        self.assertTrue((self.root / "videoFile" / "海边风景-轻音乐.mp4").is_file())
        # 状态跟着搬到新名字上，旧名字那行不留孤儿
        self.assertEqual(bgm.get_status("海边风景-轻音乐.mp4")["status"], "done")
        self.assertEqual(bgm.get_status(snap["output"])["status"], "pending")

    def test_ai_rename_without_music_clears_the_stale_row(self):
        """没合成过的素材被改名：旧名字上的残留状态行要清掉，别留给下一个同名素材。"""
        from sau_backend_xhs import _rename_material_to_title

        bgm.set_status(self.material, "failed", music_name="轻音乐", message="ffmpeg 失败")
        new_path, _ = _rename_material_to_title(
            str(self.root / "videoFile" / self.material), "海边风景"
        )

        self.assertEqual(Path(new_path).name, "海边风景.mp4")
        self.assertEqual(bgm.get_status(self.material)["status"], "pending")


class UploadTest(_MusicTestCase):
    def test_upload_and_list(self):
        music_id = self.upload_music(self.root / "short.mp3", "轻音乐")
        items = self.client.get("/music/list").get_json()["data"]
        self.assertEqual([m["name"] for m in items], ["轻音乐"])
        self.assertAlmostEqual(items[0]["duration"], 1.0, delta=0.2)
        self.assertIn("upload_time", items[0])
        self.assertEqual(items[0]["id"], music_id)
        # 文件落在 bgmFile/，而不是 videoFile/（不然发布中心会把它当素材选出来）
        self.assertTrue((self.root / "bgmFile" / items[0]["file_name"]).is_file())

    def test_rejects_non_music_extension(self):
        res = self.client.post(
            "/music/upload",
            data={"file": (io.BytesIO(b"hello"), "note.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(res.get_json()["code"], 400)

    def test_rejects_file_without_audio_track(self):
        """扩展名是 mp3、内容却不是音频：必须在**上传时**就拒掉并清理文件，
        而不是等用户点了合成、转半天才失败。"""
        res = self.client.post(
            "/music/upload",
            data={"file": (io.BytesIO(b"not really an mp3"), "fake.mp3")},
            content_type="multipart/form-data",
        )
        self.assertEqual(res.get_json()["code"], 400)
        self.assertEqual(list((self.root / "bgmFile").iterdir()), [])

    def test_delete_removes_file_and_row(self):
        music_id = self.upload_music(self.root / "short.mp3", "轻音乐")
        disk = (self.root / "bgmFile").iterdir()
        paths = list(disk)
        res = self.client.post(f"/music/{music_id}/delete", json={})
        self.assertEqual(res.get_json()["code"], 200)
        self.assertEqual(self.client.get("/music/list").get_json()["data"], [])
        for p in paths:
            self.assertFalse(p.exists(), "音乐文件应该跟着记录一起删掉")


class ComposeTest(_MusicTestCase):
    """合成主流程：换音轨 → 改名 → 落库。"""

    def test_compose_replaces_audio_track_and_renames(self):
        music_id = self.upload_music(self.root / "short.mp3", "轻音乐")
        snap = self.compose_and_wait(self.material, music_id)

        self.assertEqual(snap["state"], "done", snap)
        out_name = "测试素材-轻音乐.mp4"
        self.assertEqual(snap["output"], out_name)

        video_dir = self.root / "videoFile"
        self.assertFalse((video_dir / self.material).exists(), "原素材应该被替换掉")
        out = video_dir / out_name
        self.assertTrue(out.is_file())

        # 时长跟着视频走（音乐只有 1 秒，靠循环铺满 3 秒）
        info = bgm.probe_media(out)
        self.assertAlmostEqual(info["duration"], 3.0, delta=0.3)
        self.assertTrue(info["has_video"])
        self.assertTrue(info["has_audio"])

        # file_records 指向新名字，旧的记录不留下
        self.assertIsNone(self.file_record(self.material))
        record = self.file_record(out_name)
        self.assertIsNotNone(record)
        self.assertEqual(record["filename"], out_name)

        # 状态列：新名字是「已合成」，旧名字那行必须清掉（否则下一个同名素材会误显示已合成）
        with sqlite3.connect(self.root / "db" / "database.db") as conn:
            conn.row_factory = sqlite3.Row
            rows = {r["file_path"]: dict(r) for r in conn.execute("SELECT * FROM material_bgm")}
        self.assertNotIn(self.material, rows)
        self.assertEqual(rows[out_name]["status"], "done")
        self.assertEqual(rows[out_name]["music_name"], "轻音乐")

    @unittest.skipUnless(np, "需要 numpy 才能做频率断言")
    def test_original_audio_is_gone_not_mixed(self):
        """**这一条才是"把原音轨去掉"的证明**：成品里只能有背景音乐的频率。"""
        music_id = self.upload_music(self.root / "short.mp3", "轻音乐")
        snap = self.compose_and_wait(self.material, music_id)
        out = self.root / "videoFile" / snap["output"]

        freq = self.dominant_frequency(out)
        self.assertAlmostEqual(freq, MUSIC_A_TONE, delta=100)
        self.assertNotAlmostEqual(freq, VIDEO_TONE, delta=100)

    def test_longer_music_is_trimmed_to_video_length(self):
        music_id = self.upload_music(self.root / "long.mp3", "另一首")
        snap = self.compose_and_wait(self.material, music_id)
        out = self.root / "videoFile" / snap["output"]
        info = bgm.probe_media(out)
        self.assertAlmostEqual(info["duration"], 3.0, delta=0.3)

    def test_recompose_does_not_grow_the_filename(self):
        """用同一首音乐重合成：名字不变、原地覆盖，不会多出一个 `-轻音乐2` 的兄弟文件。"""
        music_a = self.upload_music(self.root / "short.mp3", "轻音乐")
        snap1 = self.compose_and_wait(self.material, music_a)
        snap2 = self.compose_and_wait(snap1["output"], music_a)
        self.assertEqual(snap2["output"], snap1["output"])

        files = sorted(p.name for p in (self.root / "videoFile").iterdir())
        self.assertEqual(files, ["测试素材-轻音乐.mp4"])

    def test_recompose_with_another_music_swaps_the_suffix(self):
        music_a = self.upload_music(self.root / "short.mp3", "轻音乐")
        music_b = self.upload_music(self.root / "long.mp3", "另一首")

        snap1 = self.compose_and_wait(self.material, music_a)
        snap2 = self.compose_and_wait(snap1["output"], music_b)

        self.assertEqual(snap2["output"], "测试素材-另一首.mp4")
        files = sorted(p.name for p in (self.root / "videoFile").iterdir())
        self.assertEqual(files, ["测试素材-另一首.mp4"])

    def test_uploaded_material_drops_the_uuid_prefix(self):
        """上传来的素材（磁盘名 `<uuid>_原名.mp4`、展示名 `原名.mp4`）合成后应该叫
        `原名-音乐名.mp4`，**不带 uuid 前缀**——需求里刚要求去掉 UUID 列，
        再把 uuid 塞回文件名就自相矛盾了。"""
        disk = "a1b2c3d4-0000-1111-2222-333344445555_测试素材.mp4"
        video_dir = self.root / "videoFile"
        (video_dir / self.material).rename(video_dir / disk)
        with sqlite3.connect(self.root / "db" / "database.db") as conn:
            conn.execute(
                "UPDATE file_records SET file_path = ? WHERE file_path = ?", (disk, self.material)
            )
            conn.commit()

        music_id = self.upload_music(self.root / "short.mp3", "轻音乐")
        snap = self.compose_and_wait(disk, music_id)

        self.assertEqual(snap["state"], "done", snap)
        self.assertEqual(snap["output"], "测试素材-轻音乐.mp4")
        self.assertEqual(
            sorted(p.name for p in video_dir.iterdir()), ["测试素材-轻音乐.mp4"]
        )
        self.assertEqual(self.file_record("测试素材-轻音乐.mp4")["filename"], "测试素材-轻音乐.mp4")

    def test_compose_records_failure_without_leaving_junk(self):
        """素材文件不存在时：任务标失败、状态回到「待合成」，目录里不能留半成品。"""
        res = self.client.post("/music/compose", json={"material": "不存在.mp4", "music_id": 1})
        self.assertEqual(res.get_json()["code"], 400)

        music_id = self.upload_music(self.root / "short.mp3", "轻音乐")
        (self.root / "videoFile" / self.material).unlink()
        res = self.client.post(
            "/music/compose", json={"material": self.material, "music_id": music_id}
        )
        self.assertEqual(res.get_json()["code"], 400)
        self.assertEqual([p.name for p in (self.root / "videoFile").iterdir()], [])

    def test_statuses_endpoint_reports_pending_then_done(self):
        music_id = self.upload_music(self.root / "short.mp3", "轻音乐")
        # 没合成过 = 没有记录，前端按「待合成」渲染（这里验证"不存在的素材不进表"）
        self.assertEqual(self.client.get("/music/statuses").get_json()["data"], {})

        snap = self.compose_and_wait(self.material, music_id)
        statuses = self.client.get("/music/statuses").get_json()["data"]
        self.assertEqual(statuses[snap["output"]]["status_text"], "已合成")


class Av1InMovComposeTest(_MusicTestCase):
    """`.MOV` 里装 av1 的素材：换音轨必须自动改用 mp4 封装。

    真机上踩到的坑：`-c:v copy` 把 av1 原样带进 mov 封装，ffmpeg 在**写头**阶段就被拒
    （`av1 only supported in MP4 and AVIF`），整个进程以 -22 退出——而日志末尾只剩音频
    filter 线程的 `af#0:1 ... Invalid argument`，看着像音频坏了，其实音频完全无辜。
    所以这里不看返回值，而是证明"成品真的能整条解出来"。
    """

    #: 素材名带大写 `.MOV`，和用户手里那份（GPAC 导出）一致
    MATERIAL = "av1素材.MOV"

    def _make_av1_mov(self) -> None:
        """造一条"扩展名 .MOV、视频轨是 av1"的素材。

        只能先编码成 mp4 再改名：ffmpeg 自己也不肯把 av1 写进 mov 封装，而这正是要绕开的
        那条限制。真实素材就是这么来的——容器本体就是 ISO-BMFF，扩展名叫 .mp4 还是 .MOV
        都读得出来，出问题的只是"输出时按扩展名选的 muxer"。
        """
        tmp = self.root / "av1_tmp.mp4"
        try:
            self._render_video(
                tmp,
                3,
                VIDEO_TONE,
                video_codec="libaom-av1",
                extra=["-cpu-used", "8", "-crf", "50", "-b:v", "0"],
            )
        except AssertionError as exc:  # 这个 ffmpeg 没编进 av1 编码器就跳过，不当成失败
            self.skipTest(f"这个 ffmpeg 编不了 av1: {exc}")

        tmp.replace(self.root / "videoFile" / self.MATERIAL)
        with sqlite3.connect(self.root / "db" / "database.db") as conn:
            conn.execute(
                "INSERT INTO file_records (filename, filesize, file_path) VALUES (?, ?, ?)",
                (self.MATERIAL, 1.0, self.MATERIAL),
            )
            conn.commit()

    def test_av1_in_mov_still_composes_and_stays_readable(self):
        self._make_av1_mov()
        self.assertEqual(
            bgm.probe_media(self.root / "videoFile" / self.MATERIAL)["vcodec"], "av1"
        )

        music_id = self.upload_music(self.root / "short.mp3", "轻音乐")
        snap = self.compose_and_wait(self.MATERIAL, music_id)

        self.assertEqual(snap["state"], "done", snap)
        self.assertEqual(snap["output"], "av1素材-轻音乐.MOV", "扩展名保持素材原样")
        out = self.root / "videoFile" / snap["output"]
        self.assertTrue(out.is_file())

        info = bgm.probe_media(out)
        self.assertAlmostEqual(info["duration"], 3.0, delta=0.3)
        self.assertTrue(info["has_video"])
        self.assertTrue(info["has_audio"])
        # 只是换了封装：画面流没重编码，编码还是 av1
        self.assertEqual(info["vcodec"], "av1")

        # 整条流真的能解出来——只看"文件在、探测得到轨道"是验不出空壳文件的
        proc = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(out), "-f", "null", "-"],
            capture_output=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "ignore"))
