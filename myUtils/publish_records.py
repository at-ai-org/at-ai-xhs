# -*- coding: utf-8 -*-
"""发布记录存储层（at-ai-xhs 改造新增）。

「发布记录」现在有**两个生产者**：

- 发布中心（`sau_backend_publish.py`）：素材 × 平台 × 账号 拆成任务批量发；
- 小红书工作台（`sau_backend_xhs.py`）：AI 生成文案 → 人工确认 → 单条发布。

两边写的是同一张 `publish_records` 表、同一套状态词，所以存储层抽到这里，
免得两份实现慢慢跑偏（状态叫法不一致，"发布记录"页就没法统一筛选了）。
记录里用 `source` 区分来源。

记录的生命周期
--------------
**提交/确认的那一刻就落一条**，之后按 排队中 → 发布中 → 成功/失败 更新。
这样即使进程中途被杀也有痕迹：`ensure_tables()` 在后端启动时会把残留的
「发布中/排队中」标成「已中断」——记录宁可说"没跑完"，也不能一直挂着骗人。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from conf import BASE_DIR

DB_PATH = Path(BASE_DIR) / "db" / "database.db"

# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_INTERRUPTED = "interrupted"

STATUS_TEXT: dict[str, str] = {
    STATUS_QUEUED: "排队中",
    STATUS_RUNNING: "发布中",
    STATUS_SUCCESS: "成功",
    STATUS_FAILED: "失败",
    STATUS_CANCELLED: "已取消",
    STATUS_INTERRUPTED: "已中断",
}

#: 已结束的状态（用来算完成度）
FINISHED_STATUSES = frozenset({STATUS_SUCCESS, STATUS_FAILED, STATUS_CANCELLED, STATUS_INTERRUPTED})

# --------------------------------------------------------------------------- #
# 来源
# --------------------------------------------------------------------------- #

SOURCE_PUBLISH_CENTER = "publish_center"
SOURCE_XHS_WORKBENCH = "xhs_workbench"

SOURCE_TEXT: dict[str, str] = {
    SOURCE_PUBLISH_CENTER: "发布中心",
    SOURCE_XHS_WORKBENCH: "小红书工作台",
}

#: 落库的日志行数（记录详情里能翻到）
LOG_TAIL_LINES = 150
#: 单行日志最长字符数
LOG_LINE_MAX = 500

# --------------------------------------------------------------------------- #
# 数据库
# --------------------------------------------------------------------------- #

_db_lock = threading.Lock()

#: 允许写入/更新的列（白名单，避免拼 SQL 时被注入）
RECORD_COLUMNS = frozenset(
    {
        "status",
        "stage",
        "stage_text",
        "action",
        "message",
        "title",
        "desc",
        "tags",
        "material_title",
        "account_name",
        "started_at",
        "finished_at",
        "duration",
        "steps",
        "log",
    }
)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS publish_records (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         TEXT,     -- 一次批量提交对应一个批次（工作台单条发布为空）
    task_id        TEXT,     -- 一个「素材 × 账号」任务
    source         TEXT,     -- publish_center / xhs_workbench
    platform       INTEGER,  -- 1 小红书 2 视频号 3 抖音 4 快手
    platform_name  TEXT,
    account_id     INTEGER,
    account_name   TEXT,
    account_file   TEXT,
    material       TEXT,     -- 素材磁盘文件名（videoFile/ 下的名字）
    material_title TEXT,     -- 素材展示名（素材管理里改过名就用新名字）
    title          TEXT,
    desc           TEXT,
    tags           TEXT,     -- JSON 数组
    status         TEXT,     -- queued/running/success/failed/cancelled/interrupted
    stage          TEXT,
    stage_text     TEXT,
    action         TEXT,     -- 最后一次「当前动作」（日志原文）
    message        TEXT,     -- 结果说明 / 失败原因
    scheduled      INTEGER DEFAULT 0,
    schedule_at    TEXT,     -- 定时发布的计划时间（本地时间）
    is_draft       INTEGER DEFAULT 0,
    is_original    INTEGER DEFAULT 0,
    created_at     TEXT,     -- 时间统一存本地时间字符串，方便直接看、直接筛
    started_at     TEXT,
    finished_at    TEXT,
    duration       REAL,
    steps          TEXT,     -- JSON：小红书页面操作层逐步结果
    log            TEXT      -- JSON：日志尾巴
)
"""

#: 记录详情/列表里要读的列（SELECT 用，带默认值兜底）
_RECORD_FIELDS = (
    "id, job_id, task_id, source, platform, platform_name, account_id, account_name, account_file, "
    "material, material_title, title, \"desc\", tags, status, stage, stage_text, action, message, "
    "scheduled, schedule_at, is_draft, is_original, created_at, started_at, finished_at, duration"
)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db():
    """一次连接 + 一个事务。

    不能只用 `with sqlite3.connect(...) as conn`：那个上下文只管提交/回滚，
    **不会关连接**，Web 后端跑久了会一直漏文件句柄。
    """
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _ensure_column(conn, name: str, ddl: str) -> None:
    """给老库补列。`CREATE TABLE IF NOT EXISTS` 不会动已存在的表。"""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(publish_records)")}
    if name not in columns:
        conn.execute(f"ALTER TABLE publish_records ADD COLUMN {ddl}")


def ensure_tables() -> None:
    """建表 + 补列 + 建索引，并把上次进程留下的「发布中/排队中」标成「已中断」。

    后端启动时调一次（两个蓝图都 import 本模块，所以放在 import 时执行）。
    """
    with _db_lock, db() as conn:
        conn.execute(_CREATE_TABLE)
        # `source` 是后加的列：先有发布中心、后接小红书工作台，老库得补上
        _ensure_column(conn, "source", "source TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_publish_records_created ON publish_records(created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_publish_records_platform ON publish_records(platform, account_name)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_publish_records_status ON publish_records(status)"
        )
        # `source` 是后来才加的列：之前发的记录是 NULL，而那时只有发布中心会写记录。
        # 补成 publish_center，老记录在「来源」筛选里才归位（查询那边也留了 NULL 兜底）。
        conn.execute(
            "UPDATE publish_records SET source = ? WHERE source IS NULL OR source = ''",
            (SOURCE_PUBLISH_CENTER,),
        )
        conn.execute(
            """
            UPDATE publish_records
               SET status = ?, stage_text = ?, message = ?
             WHERE status IN (?, ?)
            """,
            (
                STATUS_INTERRUPTED,
                STATUS_TEXT[STATUS_INTERRUPTED],
                "后端进程重启，任务未能跑完（浏览器操作已结束）",
                STATUS_QUEUED,
                STATUS_RUNNING,
            ),
        )


def now_text(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts if ts is not None else time.time()))


def json_safe(value):
    """把任意对象转成一定可 JSON 序列化的结构（Path / datetime 之类统一变字符串）。"""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return str(value)


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #


def new_record(**fields) -> dict:
    """构造一条待插入的记录（补默认值 + JSON 列序列化）。

    调用方给的是"业务字段"，这里负责把它们摆成表结构的样子：
    没有的列填空串/0，tags/steps/log 统一序列化成 JSON 文本。
    """
    record = {
        "job_id": fields.get("job_id") or "",
        "task_id": fields.get("task_id") or "",
        "source": fields.get("source") or SOURCE_PUBLISH_CENTER,
        "platform": int(fields.get("platform") or 0),
        "platform_name": fields.get("platform_name") or "",
        "account_id": int(fields.get("account_id") or 0),
        "account_name": fields.get("account_name") or "",
        "account_file": fields.get("account_file") or "",
        "material": fields.get("material") or "",
        "material_title": fields.get("material_title") or fields.get("material") or "",
        "title": fields.get("title") or "",
        "desc": fields.get("desc") or "",
        "tags": json.dumps(fields.get("tags") or [], ensure_ascii=False),
        "status": fields.get("status") or STATUS_QUEUED,
        "stage": fields.get("stage") or STATUS_QUEUED,
        "stage_text": fields.get("stage_text") or STATUS_TEXT[STATUS_QUEUED],
        "action": fields.get("action") or "已加入发布队列",
        "message": fields.get("message") or "",
        "scheduled": 1 if fields.get("scheduled") else 0,
        "schedule_at": fields.get("schedule_at") or "",
        "is_draft": 1 if fields.get("is_draft") else 0,
        "is_original": 1 if fields.get("is_original") else 0,
        "created_at": fields.get("created_at") or now_text(),
        "started_at": fields.get("started_at") or "",
        "finished_at": fields.get("finished_at") or "",
        "duration": fields.get("duration"),
        "steps": json.dumps(json_safe(fields.get("steps") or []), ensure_ascii=False),
        "log": json.dumps(json_safe(fields.get("log") or []), ensure_ascii=False),
    }
    return record


def insert_record(record: dict) -> int:
    """写入一条记录，返回 record_id。"""
    columns = list(record.keys())
    placeholders = ",".join("?" * len(columns))
    with _db_lock, db() as conn:
        cur = conn.execute(
            f"INSERT INTO publish_records ({','.join(columns)}) VALUES ({placeholders})",
            [record[column] for column in columns],
        )
        return int(cur.lastrowid)


def update_record(record_id: int, **fields) -> None:
    """按白名单更新记录。非白名单字段直接忽略（例如内存态才有的东西）。"""
    payload = {k: v for k, v in fields.items() if k in RECORD_COLUMNS}
    if not payload or not record_id:
        return
    assignments = ", ".join(f"{key} = ?" for key in payload)
    with _db_lock, db() as conn:
        conn.execute(
            f"UPDATE publish_records SET {assignments} WHERE id = ?",
            (*payload.values(), record_id),
        )


def record_row(row: sqlite3.Row) -> dict:
    """把数据库行转成前端友好的 dict（JSON 列解回来 + 状态中文）。"""
    data = dict(row)
    data["status_text"] = STATUS_TEXT.get(data.get("status"), data.get("status"))
    data["source_text"] = SOURCE_TEXT.get(data.get("source"), data.get("source") or "发布中心")
    for key in ("tags", "steps", "log"):
        try:
            data[key] = json.loads(data.get(key) or "[]")
        except Exception:
            data[key] = []
    return data


def select_columns() -> str:
    """列表查询用的列清单（不含 steps/log 这种大字段）。"""
    return _RECORD_FIELDS


# 建表放在 import 时做：后端启动即完成，免得第一次发完才发现表不存在。
ensure_tables()
