# -*- coding: utf-8 -*-
"""背景音乐 + 素材合成（at-ai-xhs 改造新增）。

给一条素材换背景音乐，拆成两块：

1. **音乐资产**：用户上传的音乐落在 `bgmFile/`，元数据落在 `background_music` 表；
2. **素材合成**：把视频的**原音轨整条丢掉**，换上选中的音乐，产出新文件。

为什么音乐单独放一个目录
------------------------
素材在 `videoFile/`，而发布中心的"选择素材"是**直接列该目录**的
（`sau_backend_xhs.py` 的 `/xhs/materials` 不查库）。音乐混进去就会出现在素材选择框里，
用户可能选中一个 mp3 去发布。另外音乐是长期复用的资产、素材是流水，生命周期本来也不同。

为什么不用 ffprobe
------------------
`imageio-ffmpeg` 只带 ffmpeg 一个二进制，没有 ffprobe。探测时长/轨道靠
`ffmpeg -i <file>`（不给输出文件）：它会把容器和流信息打到 stderr，然后以非 0 退出。
**这不是失败**，是 ffmpeg 的正常行为，所以这里的判定条件是"解析出了 Duration"，
不看返回码。

合成进度为什么是准的
--------------------
用 `-progress pipe:1` 让 ffmpeg 自己把 `out_time_us=` 吐到 stdout，再除以视频总时长。
不是估算，是它真正写到哪儿了。（ffmpeg 里 `out_time_ms=` 实际也是微秒——历史遗留，
所以优先读 `out_time_us`，两个都当微秒用。）
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from conf import BASE_DIR
# 复用 AI 文案那边的 ffmpeg 定位逻辑（同一个"优先 imageio-ffmpeg 自带静态二进制、
# 其次 PATH"的约定），不要在这里再写一份：两处不一致时会出现"抽帧能用、合成不能"。
from uploader.xiaohongshu_uploader.ai_copywriter import find_ffmpeg

#: 背景音乐目录（与 videoFile/ 平级，见模块开头的说明）。
BGM_DIR = Path(BASE_DIR) / "bgmFile"

#: 允许上传的音乐后缀。
ALLOWED_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wma")

#: 素材的背景音乐状态。
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

#: 状态的中文文案。失败**故意映射成「待合成」**：这一列回答的是"有没有成品"，
#: 失败原因放在 message 里（前端 tooltip 展示），不占列宽、也不多造一个状态。
STATUS_TEXT = {
    STATUS_PENDING: "待合成",
    STATUS_RUNNING: "合成中",
    STATUS_DONE: "已合成",
    STATUS_FAILED: "待合成",
}

#: 单个合成任务的上限（秒）。换音轨是"copy 视频流 + 编码音频"，通常比实时快几十倍，
#: 30 分钟只可能出现在超大素材 + 慢磁盘上；到点就杀，避免任务永远挂在「合成中」。
COMPOSE_TIMEOUT = float(os.environ.get("SAU_BGM_COMPOSE_TIMEOUT") or 30 * 60)

#: 文件名里不允许出现的字符（路径分隔符 + Windows 保留字符 + 控制字符）。
_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_STREAM_RE = re.compile(r"Stream #\d+:\d+[^:]*:\s*(Video|Audio):")


# --------------------------------------------------------------------------- #
# 数据库
# --------------------------------------------------------------------------- #


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(Path(BASE_DIR) / "db" / "database.db")


def ensure_tables(conn: sqlite3.Connection) -> None:
    """建这两张表（幂等）。

    `db/createTable.py` 里也有一份（新库一眼能看到结构），但**不能只靠它**：
    已经存在的库不会重跑那个脚本，所以每个写入口子都会先 ensure 一遍——
    和 `sau_backend_xhs.py` 的 `material_copy` 是同一套做法。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS background_music (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,              -- 展示名（不含扩展名），合成后拼在素材名后面
            file_name   TEXT NOT NULL,              -- bgmFile/ 下的磁盘文件名
            filesize    REAL,                       -- MB
            duration    REAL,                       -- 秒
            upload_time DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS material_bgm (
            file_path  TEXT PRIMARY KEY,            -- 素材磁盘文件名（改了名就跟着改）
            music_id   INTEGER,
            music_name TEXT,
            status     TEXT,                        -- running / done / failed（没有行 = 待合成）
            progress   INTEGER DEFAULT 0,
            message    TEXT,                        -- 失败原因 / 完成说明
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def ensure_music_tables() -> None:
    """启动时兜底建表（失败不致命：真正用到时每个入口还会再 ensure 一次）。"""
    try:
        with _connect() as conn:
            ensure_tables(conn)
            conn.commit()
    except Exception as exc:
        print(f"⚠️ 建背景音乐表失败（不影响启动）: {exc}")


def reset_stale_statuses() -> int:
    """启动时把「合成中」的残留状态清掉，返回清了几条。

    合成任务只活在**进程内存**里（和发布任务一样），进程一重启任务就没了，但状态行
    还停在「合成中」：界面上那一列会永远转圈，而且再点一次合成会被 409 拦住——
    等于这条素材被一个不存在的任务永久锁住。

    做法和 `publish_records.init_on_startup()` 把僵尸发布记录标成「已中断」是同一个道理：
    此刻内存里还没有任何在跑的任务，所以这些「合成中」必然是上次的残留。
    """
    try:
        with _connect() as conn:
            ensure_tables(conn)
            cur = conn.execute(
                """
                UPDATE material_bgm
                   SET status = ?, progress = 0, message = ?, updated_at = CURRENT_TIMESTAMP
                 WHERE status = ?
                """,
                (STATUS_FAILED, "上次合成被后端重启打断，请重新合成", STATUS_RUNNING),
            )
            conn.commit()
            return cur.rowcount or 0
    except Exception as exc:
        print(f"⚠️ 清理残留的合成状态失败（不影响启动）: {exc}")
        return 0


def _utc_to_local(ts) -> str:
    """SQLite 的 CURRENT_TIMESTAMP 是 UTC，直接显示会早 8 小时（东八区）。"""
    if not ts:
        return ""
    try:
        dt = datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


# --------------------------------------------------------------------------- #
# 音乐资产
# --------------------------------------------------------------------------- #


def build_disk_name(original: str) -> str:
    """给上传的音乐起磁盘名：清掉非法字符，保留扩展名，重名时补随机后缀。"""
    raw = Path(original or "").name
    ext = Path(raw).suffix.lower()
    if ext not in ALLOWED_EXTS:
        ext = ".mp3"
    stem = _ILLEGAL_RE.sub("", Path(raw).stem).strip(" .") or time.strftime("bgm_%Y%m%d_%H%M%S")
    stem = stem[:60]
    candidate = f"{stem}{ext}"
    if (BGM_DIR / candidate).exists():
        candidate = f"{stem}_{uuid.uuid4().hex[:6]}{ext}"
    return candidate


def list_music() -> list[dict]:
    """全部音乐，新的在前。"""
    out: list[dict] = []
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            ensure_tables(conn)
            for row in conn.execute("SELECT * FROM background_music ORDER BY id DESC"):
                d = dict(row)
                d["upload_time"] = _utc_to_local(d.get("upload_time"))
                d["duration"] = round(float(d.get("duration") or 0), 2)
                d["filesize"] = round(float(d.get("filesize") or 0), 2)
                out.append(d)
    except Exception as exc:
        print(f"⚠️ 读取背景音乐列表失败: {exc}")
    return out


def get_music(music_id) -> dict | None:
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            ensure_tables(conn)
            row = conn.execute("SELECT * FROM background_music WHERE id = ?", (music_id,)).fetchone()
            return dict(row) if row else None
    except Exception:
        return None


def add_music(name: str, file_name: str, filesize: float, duration: float | None) -> int | None:
    """登记一条音乐，返回新记录 id。"""
    try:
        with _connect() as conn:
            ensure_tables(conn)
            cur = conn.execute(
                "INSERT INTO background_music (name, file_name, filesize, duration) VALUES (?, ?, ?, ?)",
                (name, file_name, round(float(filesize or 0), 2), float(duration or 0)),
            )
            conn.commit()
            return cur.lastrowid
    except Exception as exc:
        print(f"⚠️ 保存背景音乐失败: {exc}")
        return None


def delete_music(music_id) -> tuple[bool, str]:
    """删一条音乐（连同磁盘文件）。已经被素材用过的成品不受影响——
    合成是把音乐**混进了新文件**，不是引用，所以删音乐不会让已合成的素材坏掉。
    """
    row = get_music(music_id)
    if not row:
        return False, "音乐不存在"
    try:
        with _connect() as conn:
            ensure_tables(conn)
            conn.execute("DELETE FROM background_music WHERE id = ?", (music_id,))
            conn.commit()
    except Exception as exc:
        return False, f"删除记录失败: {exc}"

    path = BGM_DIR / str(row.get("file_name") or "")
    try:
        if path.is_file():
            path.unlink()
    except Exception as exc:
        # 记录已经删了，文件留着也不影响使用；只提示一下
        print(f"⚠️ 音乐记录已删除，但文件未能删除: {path} ({exc})")
    return True, ""


def music_names() -> list[str]:
    """所有音乐的展示名。用来在重复合成时剥掉上一次加的 `-音乐名` 后缀。"""
    return [str(m.get("name") or "") for m in list_music() if m.get("name")]


def music_path(row: dict) -> Path:
    return BGM_DIR / str(row.get("file_name") or "")


# --------------------------------------------------------------------------- #
# 素材背景音乐状态
# --------------------------------------------------------------------------- #


def set_status(
    file_path: str,
    status: str,
    *,
    music_id=None,
    music_name: str | None = None,
    progress: int = 0,
    message: str | None = None,
) -> None:
    if not file_path:
        return
    try:
        with _connect() as conn:
            ensure_tables(conn)
            conn.execute(
                """
                INSERT INTO material_bgm (file_path, music_id, music_name, status, progress, message, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(file_path) DO UPDATE SET
                    music_id   = excluded.music_id,
                    music_name = excluded.music_name,
                    status     = excluded.status,
                    progress   = excluded.progress,
                    message    = excluded.message,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (file_path, music_id, music_name or "", status, int(progress or 0), message or ""),
            )
            conn.commit()
    except Exception as exc:
        print(f"⚠️ 记录素材背景音乐状态失败（不影响合成）: {exc}")


def get_status(file_path: str) -> dict:
    """取一条素材的状态（没有记录 = 待合成）。"""
    if not file_path:
        return status_payload(None)
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            ensure_tables(conn)
            row = conn.execute(
                "SELECT * FROM material_bgm WHERE file_path = ?", (file_path,)
            ).fetchone()
            return status_payload(dict(row) if row else None)
    except Exception:
        return status_payload(None)


def status_map() -> dict:
    out: dict = {}
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            ensure_tables(conn)
            for row in conn.execute("SELECT * FROM material_bgm"):
                d = dict(row)
                out[d["file_path"]] = status_payload(d)
    except Exception:
        pass
    return out


def status_payload(record: dict | None) -> dict:
    """把库里那一行变成给前端的形状（缺字段也永远回同一个结构）。"""
    rec = record or {}
    status = str(rec.get("status") or STATUS_PENDING)
    if status not in STATUS_TEXT:
        status = STATUS_PENDING
    return {
        "status": status,
        "status_text": STATUS_TEXT[status],
        "music_name": rec.get("music_name") or "",
        "progress": int(rec.get("progress") or 0),
        "message": rec.get("message") or "",
        "updated_at": _utc_to_local(rec.get("updated_at")),
    }


def rename_status(old_name: str, new_name: str) -> None:
    """素材改名（合成后名字会变）时把状态挪过去。"""
    if not old_name or not new_name or old_name == new_name:
        return
    try:
        with _connect() as conn:
            ensure_tables(conn)
            conn.execute("DELETE FROM material_bgm WHERE file_path = ?", (new_name,))
            conn.execute(
                "UPDATE material_bgm SET file_path = ? WHERE file_path = ?", (new_name, old_name)
            )
            conn.commit()
    except Exception as exc:
        print(f"⚠️ 同步素材背景音乐状态失败: {exc}")


def delete_status(file_path: str) -> None:
    """删素材时一起删状态。

    理由和 `material_copy` 一样：留着会在**下一个同名素材**上"复活"，
    让新素材莫名其妙显示成「已合成」。合成成功后旧名字那一行也要清掉。
    """
    if not file_path:
        return
    try:
        with _connect() as conn:
            ensure_tables(conn)
            conn.execute("DELETE FROM material_bgm WHERE file_path = ?", (file_path,))
            conn.commit()
    except Exception as exc:
        print(f"⚠️ 删除素材背景音乐状态失败（不影响素材删除）: {exc}")


# --------------------------------------------------------------------------- #
# 文件命名
# --------------------------------------------------------------------------- #


def safe_music_segment(name: str, max_len: int = 40) -> str:
    """音乐名 → 能安全拼进文件名的片段（去掉路径分隔符等非法字符）。"""
    seg = _ILLEGAL_RE.sub("", str(name or "").strip()).strip(" .")
    return seg[:max_len]


def strip_music_suffix(stem: str, names: list[str] | None = None) -> str:
    """剥掉**上一次合成**加上的 `-音乐名` 后缀。

    不剥的话，同一素材换几次音乐会变成 `标题-音乐A-音乐B-音乐C.mp4`，越滚越长；
    而且用同一首音乐重合成时，剥掉才能算出和当前文件一样的名字（原地覆盖）。
    只认"确实存在于音乐库里的名字"，避免把标题里本来就有的 `-` 误当后缀切掉。
    """
    for name in sorted({safe_music_segment(n) for n in (names or []) if n}, key=len, reverse=True):
        if name and stem.endswith("-" + name) and len(stem) > len(name) + 1:
            return stem[: -(len(name) + 1)]
    return stem


def _stem_of(name: str, ext: str) -> str:
    """去扩展名取主名。

    刻意不用 `Path.stem`：它按"最后一个点"切，名字里带点就会被切错
    （`v1.2 标题` 的 stem 是 `v1`）。这里只认调用方给出的那个扩展名。
    """
    text = str(name or "")
    if ext and text.lower().endswith(ext.lower()):
        return text[: -len(ext)]
    return text


def build_output_name(
    name: str, music_name: str, known_names: list[str] | None = None, *, ext: str | None = None
) -> str:
    """合成后的文件名：`原名-背景音乐名.扩展名`。

    这是需求里"一眼看出用的哪首音乐"的落点，所以音乐名必须**留在文件名里**，
    而不是只记在数据库里。

    `name` 给磁盘文件名或「素材管理」里的展示名都行（上传时磁盘名带 uuid 前缀、
    展示名不带）；`ext` 显式指定时以它为准——展示名偶尔可能不带后缀，
    而真正的容器格式永远以磁盘文件为准。
    """
    text = str(name or "")
    suffix = ext or Path(text).suffix or ".mp4"
    base = strip_music_suffix(_stem_of(text, suffix), known_names)
    seg = safe_music_segment(music_name) or "背景音乐"
    return f"{base}-{seg}{suffix}"


# --------------------------------------------------------------------------- #
# 探测 / 合成
# --------------------------------------------------------------------------- #


def probe_media(path) -> dict:
    """读媒体的时长和轨道情况：`{"duration": 秒|None, "has_video": bool, "has_audio": bool}`。

    只用 ffmpeg（见模块开头：没有 ffprobe），解析 `ffmpeg -i` 的 stderr。
    """
    info = {"duration": None, "has_video": False, "has_audio": False}
    ffmpeg = find_ffmpeg()
    if not ffmpeg or not Path(str(path)).is_file():
        return info
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostdin", "-i", str(path)],
            capture_output=True,
            timeout=60,
        )
    except Exception:
        return info

    text = (proc.stderr or b"").decode("utf-8", "ignore")
    m = _DURATION_RE.search(text)
    if m:
        hours, minutes, seconds = m.groups()
        info["duration"] = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    streams = _STREAM_RE.findall(text)
    info["has_video"] = "Video" in streams
    info["has_audio"] = "Audio" in streams
    return info


def compose_video_with_music(
    video_path,
    music_path,
    out_path,
    *,
    duration: float | None = None,
    on_progress: Callable[[int], None] | None = None,
    cancel_event: threading.Event | None = None,
    timeout: float = COMPOSE_TIMEOUT,
) -> tuple[bool, str]:
    """把 `video_path` 的画面 + `music_path` 的音频写成 `out_path`。

    - **原音轨整条不要**：只 `-map 0:v:0`，视频自带的音频不参与输出；
    - **音乐短了就循环**：`-stream_loop -1` 无限循环，`-t <视频时长>` 在视频结束处收尾
      （音乐比视频长时，同一条 `-t` 会把多出来的截掉）；
    - **画面不重编码**：`-c:v copy`，换音轨跟画质无关，重编码只会白白损失画质、慢几十倍。

    返回 `(是否成功, 失败原因)`。失败时会把半成品清掉——不能让一个残缺的 mp4
    留在目录里被当成素材。
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False, "找不到 ffmpeg（可 pip install imageio-ffmpeg，它自带静态二进制）"

    src = Path(str(video_path))
    music = Path(str(music_path))
    out = Path(str(out_path))

    if not src.is_file():
        return False, f"视频文件不存在: {src}"
    if not music.is_file():
        return False, f"背景音乐文件不存在: {music}"

    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return False, f"输出目录不可写: {exc}"

    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-i", str(src),
        "-stream_loop", "-1", "-i", str(music),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats",
    ]
    if duration and duration > 0:
        cmd += ["-t", f"{float(duration):.3f}"]
    else:
        # 探测不到时长时退化成"谁短听谁的"：音乐是无限循环的，所以这里一定是视频先结束
        cmd += ["-shortest"]
    cmd.append(str(out))

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
        )
    except Exception as exc:
        return False, f"启动 ffmpeg 失败: {exc}"

    stderr_lines: list[str] = []

    def _drain_stderr() -> None:
        try:
            for line in proc.stderr:  # type: ignore[union-attr]
                stderr_lines.append(line.rstrip())
                del stderr_lines[:-20]  # 只留最后 20 行，报错时够定位了
        except Exception:
            pass

    threading.Thread(target=_drain_stderr, daemon=True).start()

    state = {"timeout": False}

    def _on_timeout() -> None:
        state["timeout"] = True
        try:
            proc.kill()
        except Exception:
            pass

    watchdog = threading.Timer(timeout, _on_timeout)
    watchdog.daemon = True
    watchdog.start()

    cancelled = False
    try:
        for raw_line in proc.stdout:  # type: ignore[union-attr]
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                try:
                    proc.kill()
                except Exception:
                    pass
                break
            line = raw_line.strip()
            key, _, value = line.partition("=")
            # out_time_ms 在 ffmpeg 里其实也是微秒（历史遗留），两个都按微秒算
            if key in ("out_time_us", "out_time_ms") and value.isdigit():
                if duration and duration > 0 and on_progress:
                    seconds = int(value) / 1_000_000
                    on_progress(max(0, min(99, int(seconds / float(duration) * 100))))
        proc.wait(timeout=10)
    except Exception as exc:
        try:
            proc.kill()
        except Exception:
            pass
        remove_file_quietly(out)
        return False, f"合成中断: {exc}"
    finally:
        watchdog.cancel()

    if cancelled:
        remove_file_quietly(out)
        return False, "已取消"
    if state["timeout"]:
        remove_file_quietly(out)
        return False, f"合成超时（超过 {int(timeout)} 秒）"
    if proc.returncode != 0:
        remove_file_quietly(out)
        tail = " ｜ ".join(stderr_lines[-3:]) or "没有更多信息"
        return False, f"ffmpeg 失败（退出码 {proc.returncode}）：{tail}"
    if not out.is_file() or out.stat().st_size == 0:
        remove_file_quietly(out)
        return False, "ffmpeg 没有产出文件（可能是素材里没有视频轨）"

    if on_progress:
        on_progress(100)
    return True, ""


def remove_file_quietly(path) -> None:
    """删文件，失败只记一笔——清理动作本身不该再抛异常盖住真正的原因。"""
    try:
        target = Path(str(path))
        if target.is_file():
            target.unlink()
    except Exception as exc:
        print(f"⚠️ 清理临时文件失败: {path} ({exc})")


__all__ = [
    "ALLOWED_EXTS",
    "BGM_DIR",
    "COMPOSE_TIMEOUT",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "STATUS_TEXT",
    "add_music",
    "build_disk_name",
    "build_output_name",
    "compose_video_with_music",
    "delete_music",
    "delete_status",
    "ensure_music_tables",
    "ensure_tables",
    "find_ffmpeg",
    "get_music",
    "get_status",
    "list_music",
    "music_names",
    "music_path",
    "probe_media",
    "remove_file_quietly",
    "rename_status",
    "reset_stale_statuses",
    "safe_music_segment",
    "set_status",
    "status_map",
    "status_payload",
    "strip_music_suffix",
]
