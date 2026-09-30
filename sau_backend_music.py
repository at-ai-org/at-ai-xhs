# -*- coding: utf-8 -*-
"""背景音乐接口（at-ai-xhs 改造新增）。

路由前缀 `/music`：

- `GET  /music/list`              音乐库列表
- `POST /music/upload`            上传音乐（multipart：file / name）
- `GET  /music/<id>/file`         试听 / 播放（支持 Range，浏览器能拖动进度条）
- `POST /music/<id>/delete`       删除音乐（连同磁盘文件）
- `POST /music/compose`           给一条素材合成背景音乐 → `{ task_id }`
- `GET  /music/task/<task_id>`    合成进度（前端进度条靠它）
- `POST /music/task/<task_id>/cancel`  中断合成
- `GET  /music/statuses`          全量「素材 → 背景音乐状态」，素材管理列轮询用

合成是**原地替换**：成品写成 `原名-音乐名.mp4`，原文件删掉，`file_records` 指过去。
所以素材管理里那一行的状态会从「待合成」变成「已合成」，文件名本身也一眼能看出用的哪首音乐。
任务表放内存（进程重启即丢），和 `/xhs/task`、`/publish/task` 是同一套约定，
所以后端也**必须单进程跑**（见 Dockerfile 末尾的说明）。
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request, send_from_directory

from conf import BASE_DIR
from myUtils import background_music as bgm

music_bp = Blueprint("music", __name__, url_prefix="/music")

#: 合成任务表：`{task_id: {...}}`，内存态。
_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()

#: 中断标志：`{task_id: threading.Event}`。合成线程每收到一行 ffmpeg 进度就查一次，
#: 所以点「中断」最多半秒左右生效。
_cancel_flags: dict[str, threading.Event] = {}

#: 已结束的任务保留多久（秒）——足够用户看完成提示，又不至于无限涨。
_TASK_TTL = 3600


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def _ok(data=None, msg="ok"):
    return jsonify({"code": 200, "success": True, "msg": msg, "data": data})


def _err(msg: str, code: int = 500, data=None):
    return jsonify({"code": code, "success": False, "msg": str(msg), "data": data}), code


def _video_dir() -> Path:
    return Path(BASE_DIR) / "videoFile"


def _material_path(disk_name: str) -> Path:
    """把前端传来的素材名解析成 videoFile/ 下的文件。

    只取 basename：`../` 之类的一律在这里被削掉，和 `/getFile` 的防穿越是同一个思路。
    """
    return _video_dir() / Path(str(disk_name or "")).name


def _display_name_of(disk_name: str) -> str:
    """素材在「素材管理」里显示的名字（file_records.filename）。

    上传时磁盘名是 `<uuid>_原名.mp4`、展示名是 `原名.mp4`；合成后要拼给用户看的是
    **展示名**，否则那串 uuid 会跑到列表的文件名里——用户刚要求把 UUID 列去掉，
    再把 uuid 塞回文件名里就自相矛盾了。
    """
    try:
        with sqlite3.connect(Path(BASE_DIR) / "db" / "database.db") as conn:
            row = conn.execute(
                "SELECT filename FROM file_records WHERE file_path = ?", (disk_name,)
            ).fetchone()
            if row and (row[0] or "").strip():
                return str(row[0]).strip()
    except Exception:
        pass
    return disk_name


def _task_update(task_id: str, **fields) -> None:
    with _tasks_lock:
        if task_id in _tasks:
            _tasks[task_id].update(fields)
            _tasks[task_id]["updated_at"] = time.time()


def _task_snapshot(task_id: str) -> dict | None:
    with _tasks_lock:
        task = _tasks.get(task_id)
        if task is None:
            return None
        return {k: v for k, v in task.items() if not k.startswith("_")}


def _task_finish(task_id: str, *, state: str, message: str, output: str = "") -> None:
    with _tasks_lock:
        task = _tasks.get(task_id)
        if task is None:
            return
        task.update(
            {
                "state": state,
                "message": message,
                "output": output,
                "finished_at": time.time(),
                "updated_at": time.time(),
            }
        )


def _prune_tasks() -> None:
    """清掉放太久的已结束任务（只保留最近一小时）。"""
    now = time.time()
    with _tasks_lock:
        for tid in [
            t for t, v in _tasks.items()
            if v.get("finished_at") and now - float(v["finished_at"]) > _TASK_TTL
        ]:
            _tasks.pop(tid, None)
            _cancel_flags.pop(tid, None)


# --------------------------------------------------------------------------- #
# 音乐库
# --------------------------------------------------------------------------- #


@music_bp.get("/list")
def music_list():
    items = bgm.list_music()
    return _ok(items)


@music_bp.post("/upload")
def music_upload():
    """上传一首背景音乐。

    上传后立刻探测一次时长：探测不到（或者压根没有音频轨）的直接拒掉，
    免得"上传成功"之后再合成才发现这个文件放不出来。
    """
    file = request.files.get("file")
    if file is None or not (file.filename or "").strip():
        return _err("没有选择文件", 400)

    ext = Path(file.filename).suffix.lower()
    if ext not in bgm.ALLOWED_EXTS:
        return _err(
            f"不支持的格式 {ext or '(无扩展名)'}，只支持：{' / '.join(bgm.ALLOWED_EXTS)}", 400
        )

    try:
        bgm.BGM_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return _err(f"创建音乐目录失败: {exc}")

    disk_name = bgm.build_disk_name(file.filename)
    disk_path = bgm.BGM_DIR / disk_name
    try:
        file.save(disk_path)
    except Exception as exc:
        bgm.remove_file_quietly(disk_path)
        return _err(f"保存文件失败: {exc}")

    info = bgm.probe_media(disk_path)
    if not info.get("has_audio"):
        bgm.remove_file_quietly(disk_path)
        return _err("这个文件里没有音频轨，不能当背景音乐用（或者格式损坏了）", 400)

    # 展示名：优先用表单里填的（单个文件时前端才给），否则用原文件名（不含扩展名）
    name = (request.form.get("name") or "").strip() or Path(file.filename).stem
    name = bgm.safe_music_segment(name) or Path(disk_name).stem
    size_mb = round(disk_path.stat().st_size / 1048576, 2)

    music_id = bgm.add_music(name, disk_name, size_mb, info.get("duration"))
    if not music_id:
        bgm.remove_file_quietly(disk_path)
        return _err("写库失败，音乐未保存")

    return _ok(
        {
            "id": music_id,
            "name": name,
            "file_name": disk_name,
            "filesize": size_mb,
            "duration": round(float(info.get("duration") or 0), 2),
        },
        "上传成功",
    )


@music_bp.get("/<int:music_id>/file")
def music_file(music_id: int):
    """播放/试听。

    走 `send_from_directory`（`conditional=True`）而不是自己读文件：
    它会把 Range 请求处理好，浏览器才能拖动音频进度条。
    前端记得带 token（`<audio src>` 用 `withToken()` 拼查询串）。
    """
    row = bgm.get_music(music_id)
    if not row:
        return _err("音乐不存在", 404)
    path = bgm.music_path(row)
    if not path.is_file():
        return _err("音乐文件已丢失（可能被手动删了）", 404)
    return send_from_directory(bgm.BGM_DIR, path.name, conditional=True)


@music_bp.post("/<int:music_id>/delete")
def music_delete(music_id: int):
    ok, msg = bgm.delete_music(music_id)
    if not ok:
        return _err(msg, 404)
    return _ok({"id": music_id}, "已删除")


@music_bp.get("/statuses")
def music_statuses():
    """全量状态表：`{素材磁盘名: {status, status_text, music_name, progress, message}}`。

    素材管理页在合成期间轮询它来刷新「背景音乐」这一列，比整表重拉 `/getFiles` 便宜。
    """
    return _ok(bgm.status_map())


# --------------------------------------------------------------------------- #
# 合成
# --------------------------------------------------------------------------- #


@music_bp.post("/compose")
def music_compose():
    """给一条素材换背景音乐（异步，立刻返回 task_id）。

    同步做的话，一条几百 MB 的素材要等几十秒，浏览器和反代都可能先超时。
    """
    body = request.get_json(silent=True) or {}
    material = str(body.get("material") or "").strip()
    music_id = body.get("music_id")

    if not material:
        return _err("缺少 material（素材磁盘文件名）", 400)

    video_path = _material_path(material)
    if not video_path.is_file():
        return _err(f"素材文件不存在: {video_path}", 400)

    row = bgm.get_music(music_id)
    if not row:
        return _err("背景音乐不存在（可能已被删除）", 400)
    music_path = bgm.music_path(row)
    if not music_path.is_file():
        return _err(f"背景音乐文件已丢失: {music_path}", 400)

    # 状态行还停在「合成中」= 上次那个进程还没跑完 / 被 Ctrl-C 打断了。
    # 内存里的任务已经没了（重启就没了），只能靠库里这行拦住重复提交。
    if bgm.get_status(video_path.name).get("status") == bgm.STATUS_RUNNING:
        return _err("这条素材的状态还是「合成中」（后端可能刚重启过），请先刷新列表确认", 409)

    info = bgm.probe_media(video_path)
    if not info.get("has_video"):
        return _err("这条素材里没有视频轨，无法合成背景音乐", 400)

    # 同一个素材不许重复起任务：两个 ffmpeg 同时写同一个文件名，结果不可预期。
    # 两道检查各挡一类情况：
    #   * 库里的 running —— 后端重启后内存任务没了，但状态行还留着（上次的残留）；
    #   * 内存里的活动任务 —— 连着点两下按钮时，"读状态→写状态"之间有个窗口，
    #     只靠库判断两次请求可能都过。所以"查重 + 建任务"必须在同一把锁里完成。
    _prune_tasks()
    task_id = uuid.uuid4().hex
    cancel_flag = threading.Event()
    now = time.time()
    with _tasks_lock:
        if any(
            t.get("material") == video_path.name and not t.get("finished_at")
            for t in _tasks.values()
        ):
            return _err("这条素材正在合成中，请等它结束", 409)
        _tasks[task_id] = {
            "task_id": task_id,
            "material": video_path.name,
            "music_id": row.get("id"),
            "music_name": row.get("name") or "",
            "state": "queued",
            "progress": 0,
            "message": "排队中…",
            "output": "",
            "duration": info.get("duration") or 0,
            "had_audio": bool(info.get("has_audio")),
            "created_at": now,
            "updated_at": now,
        }
    _cancel_flags[task_id] = cancel_flag

    # 立刻把列表那一行标成「合成中」：用户点了按钮就该看到反馈，
    # 不用等线程调度起来再等第一次轮询。
    bgm.set_status(
        video_path.name,
        bgm.STATUS_RUNNING,
        music_id=row.get("id"),
        music_name=row.get("name") or "",
        progress=0,
        message="",
    )

    threading.Thread(target=_compose_worker, args=(task_id,), daemon=True).start()
    return _ok({"task_id": task_id, "material": video_path.name})


@music_bp.get("/task/<task_id>")
def music_task(task_id: str):
    snapshot = _task_snapshot(task_id)
    if snapshot is None:
        return _err("任务不存在（后端可能已重启，或任务太久已被清理）", 404)
    return _ok(snapshot)


@music_bp.post("/task/<task_id>/cancel")
def music_task_cancel(task_id: str):
    snapshot = _task_snapshot(task_id)
    if snapshot is None:
        return _err("任务不存在", 404)
    if snapshot.get("state") not in ("queued", "running"):
        return _err(f"任务已经结束了（{snapshot.get('state')}），不用中断", 400)
    flag = _cancel_flags.get(task_id)
    if flag is not None:
        flag.set()
    _task_update(task_id, message="正在中断…")
    return _ok({"task_id": task_id})


def _compose_worker(task_id: str) -> None:
    """合成线程：换音轨 → 改名 → 同步数据库。

    任何一步失败都要把状态写成 `failed` 并且**不留下半成品文件**，
    否则素材管理里会多出一条打不开的 mp4。
    """
    snapshot = _task_snapshot(task_id)
    if snapshot is None:
        return

    material = snapshot["material"]
    music_row = bgm.get_music(snapshot.get("music_id"))
    if not music_row:
        _fail(task_id, material, "背景音乐不存在（可能已被删除）")
        return

    video_path = _material_path(material)
    music_path = bgm.music_path(music_row)
    duration = float(snapshot.get("duration") or 0) or None
    music_name = str(music_row.get("name") or "")
    ext = video_path.suffix or ".mp4"

    _task_update(task_id, state="running", message="正在合成…", progress=0)

    # 临时文件**必须放在 videoFile/ 里**：最后一步要 os.replace 成正式文件，
    # 跨文件系统 rename 会抛 EXDEV（容器里 videoFile 常是单独的挂载卷）。
    # 用点号开头：/xhs/materials 会跳过隐藏文件，不会把它当成素材列出来。
    temp_path = _video_dir() / f".bgm_tmp_{task_id}{ext}"
    flag = _cancel_flags.get(task_id)

    last_saved = {"progress": -1}

    def _on_progress(percent: int) -> None:
        _task_update(task_id, progress=percent)
        # 数据库每 5% 写一次就够了，别让进度上报把 sqlite 写爆
        if percent // 5 != last_saved["progress"] // 5:
            last_saved["progress"] = percent
            bgm.set_status(
                material,
                bgm.STATUS_RUNNING,
                music_id=music_row.get("id"),
                music_name=music_name,
                progress=percent,
                message="",
            )

    ok, err = bgm.compose_video_with_music(
        video_path,
        music_path,
        temp_path,
        duration=duration,
        on_progress=_on_progress,
        cancel_event=flag,
    )
    if not ok:
        _fail(task_id, material, err, music_row=music_row)
        return

    # ---- 换名 + 落库 ---------------------------------------------------------
    try:
        known = bgm.music_names()
        # 名字以**展示名**为基准（上传时磁盘名带 uuid 前缀，展示名不带），
        # 扩展名以磁盘文件为准；合成后磁盘名和展示名就是同一个字符串了。
        out_name = bgm.build_output_name(
            _display_name_of(material), music_name, known, ext=Path(material).suffix
        )
        target = _video_dir() / out_name
        # 同名文件已存在、且不是它自己：多半是另一条素材正好叫这个名字，补个序号别覆盖人家
        if target.name != material and target.exists():
            target = _unique_target(target)

        if target.name == material:
            # 同名字符串（用同一首音乐重合成）→ 原子覆盖
            temp_path.replace(target)
        else:
            temp_path.replace(target)
            try:
                video_path.unlink()
            except Exception as exc:
                # 旧的没删掉不影响正确性（列表按 file_records 走，不再指向它），但要留痕
                print(f"⚠️ 合成后删除原素材失败（文件仍在磁盘上）: {video_path} ({exc})")

        # 这两个是素材的"附属数据"，以文件名为主键，改名后必须跟着搬，否则
        # AI 文案会显示成"无"、状态行会留在旧名字上。
        # 延迟 import：sau_backend_xhs 会把整个上传器栈拉进来，本模块被单独 import
        # （测试、脚本）时不该付这个代价。
        from sau_backend_xhs import rename_material_copy, sync_file_record

        rename_material_copy(material, target.name)
        sync_file_record(material, target.name, target.name)

        if target.name != material:
            bgm.delete_status(material)
        bgm.set_status(
            target.name,
            bgm.STATUS_DONE,
            music_id=music_row.get("id"),
            music_name=music_name,
            progress=100,
            message=f"已合成为「{target.name}」",
        )
    except Exception as exc:
        _fail(task_id, material, f"合成完成但改名/落库失败：{exc}", music_row=music_row)
        return

    _task_finish(
        task_id,
        state="done",
        message=f"合成完成：{target.name}",
        output=target.name,
    )


def _unique_target(target: Path) -> Path:
    """同名时补序号：`标题-音乐2.mp4`。"""
    for n in range(2, 100):
        candidate = target.with_name(f"{target.stem}{n}{target.suffix}")
        if not candidate.exists():
            return candidate
    return target.with_name(f"{target.stem}_{uuid.uuid4().hex[:6]}{target.suffix}")


def _fail(task_id: str, material: str, message: str, music_row: dict | None = None) -> None:
    """统一收尾：任务标失败，素材状态退回「待合成」（原因留在 message 里）。"""
    _task_finish(task_id, state="failed", message=message)
    bgm.set_status(
        material,
        bgm.STATUS_FAILED,
        music_id=(music_row or {}).get("id"),
        music_name=(music_row or {}).get("name") or "",
        progress=0,
        message=message,
    )
    print(f"⚠️ 背景音乐合成失败（{material}）: {message}")


__all__ = ["music_bp"]
