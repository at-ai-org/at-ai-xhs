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
这样即使进程中途被杀也有痕迹：服务进程启动时 `init_on_startup()` 会把残留的
「发布中/排队中」标成「已中断」——记录宁可说"没跑完"，也不能一直挂着骗人。

注意那一步**不挂在 import 上**：它是破坏性写入，挂在 import 上会让任何
`import myUtils.publish_records` 的脚本把正在跑的任务判成中断（见该函数的说明）。
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

#: 「这个账号确实把这条素材发出去了」——再发一次就是重复发布
PUBLISHED_STATUSES = frozenset({STATUS_SUCCESS})
#: 「还在路上」——结论没落定，但同一账号再提交一次同样是重复
IN_FLIGHT_STATUSES = frozenset({STATUS_QUEUED, STATUS_RUNNING})
#: 查重复发布时关心的状态。失败/取消/中断**不算**：那种情况下重发是正常的
REPEAT_CHECK_STATUSES = PUBLISHED_STATUSES | IN_FLIGHT_STATUSES

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
    """建表 + 补列 + 建索引。**纯 schema 操作，一个字节的业务数据都不改。**

    放在 import 时执行（两个蓝图都 import 本模块，后端起来表就已经在了）。

    这里**刻意不含任何数据写入**：写数据挂在 import 上是很危险的——任何脚本只要
    `import myUtils.publish_records`（调试脚本、维护工具、REPL、我自己的验证命令）
    就会顺手动到**正在发布**的那份库。原先那两句 UPDATE（回填 source、把残留任务
    标成已中断）正是这么把我手上一条在跑的任务误标成"已中断"的。

    数据修正统一挪到 `init_on_startup()`，只由服务进程启动时显式调用一次。
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


def backfill_source_column() -> int:
    """把 `source` 为空的老记录补成「发布中心」，返回改动条数。

    `source` 是后来才加的列：在它存在之前只有发布中心会写记录，所以空值一律归它
    （查询那边也留了 NULL 兜底，这里只是让「来源」筛选看起来对）。
    """
    with _db_lock, db() as conn:
        cursor = conn.execute(
            "UPDATE publish_records SET source = ? WHERE source IS NULL OR source = ''",
            (SOURCE_PUBLISH_CENTER,),
        )
        return cursor.rowcount or 0


def mark_orphan_records_interrupted(reason: str | None = None) -> int:
    """把上一个进程留下的「排队中/发布中」记录标成「已中断」，返回改动条数。

    **只应该在服务进程启动时调用一次**（见 `sau_backend.py` 的 `__main__`）。
    进程刚起来时内存里没有任何在跑的任务，所以此刻所有 queued/running 都必然是
    上次留下的僵尸记录。

    反过来，如果在一个**正在发布**的进程之外调用它（哪怕是 `import` 本模块），
    就会把活着的任务误判成中断——这正是它不能挂在 `ensure_tables()` 上的原因。
    """
    reason = reason or "后端进程重启，任务未能跑完（浏览器操作已结束）"
    with _db_lock, db() as conn:
        cursor = conn.execute(
            """
            UPDATE publish_records
               SET status = ?, stage_text = ?, message = ?, finished_at = ?
             WHERE status IN (?, ?)
            """,
            (
                STATUS_INTERRUPTED,
                STATUS_TEXT[STATUS_INTERRUPTED],
                reason,
                now_text(),
                STATUS_QUEUED,
                STATUS_RUNNING,
            ),
        )
        return cursor.rowcount or 0


def init_on_startup() -> dict[str, int]:
    """服务进程启动时调一次：补老数据 + 收尾上次残留的任务。返回各自改动条数。

    这是**唯一**该做数据修正的入口。调用点是 `sau_backend.py` 的 `__main__`——
    只有真正跑服务时才执行，import、测试、维护脚本都不会误伤正在发布的任务。
    """
    return {
        "source_backfilled": backfill_source_column(),
        "orphans_interrupted": mark_orphan_records_interrupted(),
    }



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


# --------------------------------------------------------------------------- #
# 重复发布检查
# --------------------------------------------------------------------------- #


def find_repeat_publishes(targets: list[dict], materials: list[str]) -> list[dict]:
    """查这批素材在指定「平台 × 账号」下是不是已经发过了。

    `targets` 每项形如 `{"platform": 1, "account_id": 3, "account_name": "账号A"}`，
    `materials` 是素材名（`videoFile/` 下的文件名）。

    发布中心拿它做「同账号已发过的素材再发一次」的二次确认，**只给数据、不做拦截**：
    重发有时确实是用户想要的（比如上次发出去的效果不好想再发一次），所以这里只负责
    把"哪条素材在哪个账号发过、什么时候发的"查出来，让界面去问人。

    两个匹配口径都是**故意放宽**的，宁可多报一条让人多看一眼，也不能漏报：

    * 素材同时按 `material` 和 `material_title` 匹配 —— 素材生成 AI 文案时会被改名
      （见 `sau_backend_xhs._rename_material_to_title`），而记录里存的是**发布当时**
      的名字。只按其中一个查，改名前后就互相看不见了，而这恰恰是同一条素材最容易被
      重复发出去的情形；
    * 账号同时按 `account_id` 和 `account_name` 匹配 —— 小红书工作台写记录时刻意留空
      `account_id`（它那套账号和发布中心的 `user_info` 不是一套，见
      `sau_backend_xhs.py`），只按 id 查就会把它发过的那条漏掉。

    返回的每一项对应一个「素材 × 目标账号」组合，只有真的发过（或正在发）才会出现。
    `published` 指有过成功记录，`in_flight` 指还有排队中/发布中的记录。
    """
    names: list[str] = []
    for name in materials or []:
        text = str(name or "").strip()
        if text and text not in names:
            names.append(text)
    if not names or not targets:
        return []

    name_slots = ",".join("?" * len(names))
    status_slots = ",".join("?" * len(REPEAT_CHECK_STATUSES))
    statuses = sorted(REPEAT_CHECK_STATUSES)
    found: dict[tuple, dict] = {}
    #: 同一个（平台，账号）只查一次——一个账号可能被传进来好几遍（每个素材一条任务）
    done: set[tuple] = set()

    for target in targets or []:
        try:
            platform = int(target.get("platform") or 0)
            account_id = int(target.get("account_id") or 0)
        except (TypeError, ValueError):
            continue
        account_name = str(target.get("account_name") or "").strip()
        if not platform or not (account_id or account_name):
            continue
        if (platform, account_id, account_name) in done:
            continue
        done.add((platform, account_id, account_name))

        where = [
            "platform = ?",
            f"status IN ({status_slots})",
            f"(material IN ({name_slots}) OR material_title IN ({name_slots}))",
        ]
        params: list = [platform, *statuses, *names, *names]
        if account_id and account_name:
            where.append("(account_id = ? OR account_name = ?)")
            params.extend([account_id, account_name])
        elif account_id:
            where.append("account_id = ?")
            params.append(account_id)
        else:
            where.append("account_name = ?")
            params.append(account_name)

        with db() as conn:
            rows = conn.execute(
                f"""
                SELECT id, job_id, source, platform, platform_name, account_name,
                       material, material_title, title, status, created_at
                  FROM publish_records
                 WHERE {' AND '.join(where)}
                 ORDER BY id DESC
                """,
                params,
            ).fetchall()

        for row in rows:
            # 一条记录可能同时命中 material 和 material_title（正常情况下两者相同），
            # 去重后再登记，免得同一条记录被算成两次
            for name in dict.fromkeys((row["material"], row["material_title"])):
                if name not in names:
                    continue
                key = (platform, account_id, name)
                entry = found.get(key)
                if entry is None:
                    entry = found[key] = {
                        "platform": platform,
                        "platform_name": row["platform_name"] or "",
                        "account_id": account_id,
                        "account_name": account_name or row["account_name"] or "",
                        "material": name,
                        "material_title": row["material_title"] or name,
                        "count": 0,
                        "published_count": 0,
                        "in_flight_count": 0,
                        "last_record_id": 0,
                        "last_job_id": "",
                        "last_title": "",
                        "last_status": "",
                        "last_time": "",
                        "last_source": "",
                        "last_success_time": "",
                    }
                entry["count"] += 1
                if row["status"] in PUBLISHED_STATUSES:
                    entry["published_count"] += 1
                    if not entry["last_success_time"]:
                        entry["last_success_time"] = row["created_at"] or ""
                else:
                    entry["in_flight_count"] += 1
                # rows 按 id 倒序，所以第一条就是最近的那条
                if not entry["last_time"]:
                    entry["last_record_id"] = int(row["id"])
                    entry["last_job_id"] = row["job_id"] or ""
                    entry["last_title"] = row["title"] or ""
                    entry["last_status"] = row["status"] or ""
                    entry["last_time"] = row["created_at"] or ""
                    entry["last_source"] = row["source"] or SOURCE_PUBLISH_CENTER

    result: list[dict] = []
    for entry in found.values():
        entry["published"] = entry["published_count"] > 0
        entry["in_flight"] = entry["in_flight_count"] > 0
        entry["last_status_text"] = STATUS_TEXT.get(entry["last_status"], entry["last_status"])
        entry["last_source_text"] = SOURCE_TEXT.get(entry["last_source"], entry["last_source"])
        # 一句话说清"怎么个重复法"。措辞要能直接接在"这个账号"后面念
        # （任务日志是 `重复发布：这个账号{message}`），也要能单独当表格里的一列
        if entry["published"]:
            message = f"已于 {entry['last_success_time']} 成功发布过"
            if entry["in_flight"]:
                message += f"；{entry['last_time']} 又提交了一条，现在还是「{entry['last_status_text']}」"
        else:
            message = f"于 {entry['last_time']} 提交过一条，现在还是「{entry['last_status_text']}」"
        entry["message"] = message
        result.append(entry)

    result.sort(key=lambda item: (item["platform"], item["account_name"], item["material"]))
    return result


# 建表放在 import 时做：后端启动即完成，免得第一次发完才发现表不存在。
# 这里只建 schema、不动业务数据；把残留任务标成"已中断"是破坏性写入，
# 由服务进程启动时显式调用 mark_orphan_records_interrupted()，不能挂在这里。
ensure_tables()
