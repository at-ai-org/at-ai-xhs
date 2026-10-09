# -*- coding: utf-8 -*-
"""账号数据的落库与缓存（at-ai-xhs 改造新增）。

为什么必须缓存
--------------
抓一次账号数据要**开一次浏览器**（小红书实测 5~10 秒：启动 chromium → 登录态进
创作服务平台 → 逐接口签名请求）。这个代价放在"点一下详情按钮"上是可以接受的，
但放在"打开账号管理页"上就不行——页面上有 N 个账号，一进页面就开 N 个浏览器，
用户会以为工具卡死了。

所以策略是 **缓存优先 + 手动刷新**：

* `GET /account-stats` 默认走缓存（`TTL_SECONDS` 之内直接返回，不碰浏览器）；
* 缓存过期了也不让用户干等——把**旧数据连同 `stale: true`** 一起返回，
  界面上标一行"数据是 X 前的"，用户可以自己点「刷新」；
* 点「刷新」= `?refresh=1`，同时**顺手把旧数据带回去**（`stale` 那个字段），
  这样弹窗能立刻渲染出上次的数字、再换成新的，而不是先白屏再出数。

为什么要落库（而不是内存 dict）
------------------------------
1. **重启不丢**。后端重启后账号管理页第一眼就有上次的数据；
2. **多进程/多线程一致**。Flask 这边是线程池 + 可能的多次请求，SQLite 是唯一
   可靠的汇合点；内存 dict 在 `threaded=True` 下要靠额外的锁保护，反而更容易写错；
3. **可追溯**。`fetched_at` 落盘之后，"这个数字是什么时候的"是可查的，
   而不是界面上飘着一个不知道什么时候抓的值。

表结构
------
一张 `account_stats`，主键是账号 id（`user_info.id`）。一个账号一行，
新抓的覆盖旧的——**刻意不存历史**：这是"账号当前状态"的快照，不是时序库。
要看趋势，小红书自己返回了 `series`（逐日涨粉），那里才是趋势。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from conf import BASE_DIR

DB_PATH = Path(BASE_DIR) / "db" / "database.db"

#: 缓存多久算"新鲜"（秒）。默认 30 分钟。
#:
#: 平台侧的数据本身就是**按天/按小时**聚合的（小红书数据中心一天更新几次），
#: 缓存几分钟和缓存半小时在观感上没有区别，但后者能让用户来回点详情时
#: 完全不碰浏览器。要立刻看到最新的就手动点「刷新」。
TTL_SECONDS = 30 * 60

#: 同一个账号同时只抓一次。
#:
#: 用户可能连点两下「刷新」，或者一边手动刷新一边列表在自动拉——
#: 不加这把锁就会同时开两个浏览器抓同一个账号，既慢又更容易触发平台风控
#: （同一个账号短时间两次登录态访问，正是风控最敏感的模式）。
_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def account_lock(account_id: int) -> threading.Lock:
    """取某个账号的抓取锁（同一个 id 永远拿到同一个锁对象）。"""
    key = int(account_id)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables() -> None:
    """建表（幂等）。老库不会重跑 `db/createTable.py`，所以启动时兜底建一遍。

    和 `background_music.ensure_music_tables()`、`publish_records._ensure_tables()`
    是同一套做法：谁用表谁负责建，不要求用户去找 `db/createTable.py`。
    """
    try:
        with _connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS account_stats (
                    account_id  INTEGER PRIMARY KEY,
                    platform_no INTEGER,
                    account_name TEXT,
                    stats       TEXT,
                    fetched_at  TEXT,
                    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_account_stats_platform "
                "ON account_stats(platform_no)"
            )
    except Exception as exc:  # 建表失败不该让整个后端起不来
        print(f"⚠️ 建 account_stats 表失败（账号详情页会一直拿不到缓存）: {exc}")


def _row_to_record(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        stats = json.loads(row["stats"] or "{}")
    except Exception:
        return None
    return {
        "account_id": row["account_id"],
        "platform_no": row["platform_no"],
        "account_name": row["account_name"],
        "stats": stats,
        "fetched_at": row["fetched_at"],
    }


def load(account_id: int) -> dict[str, Any] | None:
    """读缓存。没有就返回 None。"""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM account_stats WHERE account_id = ?", (int(account_id),)
            ).fetchone()
        return _row_to_record(row)
    except Exception as exc:
        print(f"⚠️ 读账号数据缓存失败（account_id={account_id}）: {exc}")
        return None


def load_many(account_ids: list[int]) -> dict[int, dict[str, Any]]:
    """批量读缓存，返回 `{account_id: record}`。

    列表页要"每个账号顺带显示粉丝数"时用这个一次读完，避免 N 次查询。
    """
    ids = [int(i) for i in account_ids if i is not None]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    try:
        with _connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM account_stats WHERE account_id IN ({placeholders})", ids
            ).fetchall()
    except Exception as exc:
        print(f"⚠️ 批量读账号数据缓存失败: {exc}")
        return {}
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        record = _row_to_record(row)
        if record:
            out[record["account_id"]] = record
    return out


def save(
    account_id: int,
    platform_no: int,
    account_name: str,
    stats: dict[str, Any],
    fetched_at: str | None = None,
) -> None:
    """写缓存（新数据覆盖旧的）。写失败只打日志，不往上抛。

    理由同 `ensure_tables()`：缓存写不进去是"下次还得重新抓一遍"，
    不该让用户这一眼的数据也看不成。
    """
    fetched_at = fetched_at or stats.get("fetched_at") or time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO account_stats
                    (account_id, platform_no, account_name, stats, fetched_at, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(account_id) DO UPDATE SET
                    platform_no  = excluded.platform_no,
                    account_name = excluded.account_name,
                    stats        = excluded.stats,
                    fetched_at   = excluded.fetched_at,
                    updated_at   = CURRENT_TIMESTAMP
                """,
                (
                    int(account_id),
                    int(platform_no or 0),
                    account_name or "",
                    json.dumps(stats, ensure_ascii=False),
                    fetched_at,
                ),
            )
    except Exception as exc:
        print(f"⚠️ 写账号数据缓存失败（account_id={account_id}）: {exc}")


def delete(account_id: int) -> None:
    """删掉某个账号的缓存（账号被删除时调用，避免 id 复用后串数据）。

    为什么必须删：`user_info.id` 是 `AUTOINCREMENT`，正常不会复用；但用户删号后
    再导入同一份数据库、或者手工改过 id 的情况下会。留着旧行意味着新账号一进来
    就显示上一个账号的粉丝数——这种错很难被发现，直接删掉最省事。
    """
    try:
        with _connect() as conn:
            conn.execute("DELETE FROM account_stats WHERE account_id = ?", (int(account_id),))
    except Exception as exc:
        print(f"⚠️ 删除账号数据缓存失败（account_id={account_id}）: {exc}")


def age_seconds(fetched_at: str | None) -> float | None:
    """`fetched_at`（`YYYY-MM-DD HH:MM:SS`，本地时间）到现在过了多少秒。

    解析不了就返回 None——调用方据此当作"不知道有多旧"，而不是当作"刚刚抓的"。
    """
    if not fetched_at:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            from datetime import datetime

            return max(0.0, (datetime.now() - datetime.strptime(fetched_at, fmt)).total_seconds())
        except Exception:
            continue
    return None


def is_fresh(record: dict[str, Any] | None, ttl: float = TTL_SECONDS) -> bool:
    """缓存还在 TTL 内吗。没有缓存 / 时间读不出来 → 都算不新鲜。"""
    if not record:
        return False
    age = age_seconds(record.get("fetched_at"))
    return age is not None and age < ttl


__all__ = [
    "DB_PATH",
    "TTL_SECONDS",
    "account_lock",
    "age_seconds",
    "delete",
    "ensure_tables",
    "is_fresh",
    "load",
    "load_many",
    "save",
]
