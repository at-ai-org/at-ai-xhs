#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合并 `user_info` 里的重复账号（同一个「平台 + 账号名」出现多行）。

由来
----
`myUtils/login.py` 里四个平台的扫码登录成功后原来都是裸 `INSERT`，没有去重。
于是「重新扫码」不会更新原记录，而是插出一条新记录，旧的那条继续留着报「异常」——
界面上就出现两个同名账号（旧记录的 cookie 其实已经死了）。

该 bug 本身已经在 `_record_account()` 里修好了（幂等 upsert），**下次扫码会自动合并**。
这个脚本是用来处理"用户不打算再扫一次"的存量重复行，顺便看一眼有没有别的不一致。

合并规则（与 `_record_account` 保持一致）
----------------------------------------
* 每个「平台 + 账号名」只保留一行，**保留 id 最小的那条**。
  不能反过来"删旧留新"：`publish_records.account_id` 引用着 `user_info.id`，
  删掉旧 id 会让历史发布记录被判定成「账号不存在（可能已被删除）」。
* 保留行的 `filePath` 指向**最新一次扫码产生的 cookie**（id 最大那条的 filePath），
  这样合并不等于把用户踢回死 cookie。
* 被删掉的重复行若有历史发布记录，先把这些记录改指到保留行，再删——顺序反了会断链。

用法
----
    python tools/dedupe_accounts.py            # 干跑，只报告不写库
    python tools/dedupe_accounts.py --apply    # 真的合并（会先自动备份数据库）
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conf import BASE_DIR  # noqa: E402

DB_PATH = Path(BASE_DIR) / "db" / "database.db"

PLATFORM_NAMES = {1: "小红书", 2: "视频号", 3: "抖音", 4: "快手"}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def find_duplicates(conn: sqlite3.Connection) -> dict[tuple[int, str], list[dict]]:
    """按 (平台, 账号名) 分组，返回存在多行的那些组。"""
    rows = conn.execute(
        "SELECT id, type, filePath, userName, status FROM user_info ORDER BY id"
    ).fetchall()
    groups: dict[tuple[int, str], list[dict]] = {}
    for row in rows:
        groups.setdefault((row[1], row[3]), []).append(
            {"id": row[0], "type": row[1], "filePath": row[2], "name": row[3], "status": row[4]}
        )
    return {k: v for k, v in groups.items() if len(v) > 1}


def described_plan(conn: sqlite3.Connection, groups: dict) -> list[str]:
    """把要做的改动描述成人能看懂的行，干跑和真跑共用。"""
    lines: list[str] = []
    has_records = _table_exists(conn, "publish_records")

    for (platform, name), rows in sorted(groups.items()):
        keep, duplicates = rows[0], rows[1:]
        # 最新一次扫码的 cookie 挂在 id 最大的那条上
        fresh = max(rows, key=lambda r: r["id"])

        lines.append(
            f"  [{PLATFORM_NAMES.get(platform, platform)}] {name}"
        )
        lines.append(
            f"      保留 id={keep['id']}（cookie 由 {keep['filePath']} "
            f"改指到 {fresh['filePath']}，状态置为正常）"
        )
        for dup in duplicates:
            note = ""
            if has_records:
                n = conn.execute(
                    "SELECT COUNT(*) FROM publish_records WHERE account_id = ?", (dup["id"],)
                ).fetchone()[0]
                if n:
                    note = f"，其 {n} 条发布记录改指到 id={keep['id']}"
            lines.append(f"      删除 id={dup['id']}（cookie={dup['filePath']}）{note}")
    return lines


def apply_plan(conn: sqlite3.Connection, groups: dict) -> None:
    has_records = _table_exists(conn, "publish_records")

    for (platform, name), rows in sorted(groups.items()):
        keep, duplicates = rows[0], rows[1:]
        fresh = max(rows, key=lambda r: r["id"])

        conn.execute(
            "UPDATE user_info SET filePath = ?, status = 1 WHERE id = ?",
            (fresh["filePath"], keep["id"]),
        )
        for dup in duplicates:
            if has_records:
                conn.execute(
                    "UPDATE publish_records SET account_id = ? WHERE account_id = ?",
                    (keep["id"], dup["id"]),
                )
            conn.execute("DELETE FROM user_info WHERE id = ?", (dup["id"],))
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description="合并 user_info 里的重复账号")
    parser.add_argument("--apply", action="store_true", help="真的写库（默认只干跑）")
    parser.add_argument("--db", default=str(DB_PATH), help=f"数据库路径（默认 {DB_PATH}）")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"❌ 找不到数据库: {db_path}")
        return 1

    if args.apply:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = db_path.with_name(f"{db_path.name}.bak-{stamp}")
        shutil.copy2(db_path, backup)
        print(f"💾 已备份到 {backup}")

    with sqlite3.connect(db_path) as conn:
        groups = find_duplicates(conn)
        if not groups:
            print("✅ 没有重复账号，无需处理")
            return 0

        total_rows = sum(len(v) - 1 for v in groups.values())
        print(f"🔍 发现 {len(groups)} 组重复，涉及 {total_rows} 条多余记录：")
        for line in described_plan(conn, groups):
            print(line)
        print()

        if not args.apply:
            print("（干跑结束，未写库。确认无误后加 --apply 真正执行）")
            return 0

        apply_plan(conn, groups)
        print(f"✅ 已合并，删除 {total_rows} 条多余记录")

        remaining = find_duplicates(conn)
        if remaining:
            print(f"⚠️ 仍有 {len(remaining)} 组重复，请检查")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
