# -*- coding: utf-8 -*-
"""账号数据（粉丝 / 互动）的 Web 接口（at-ai-xhs 改造新增）。

路由前缀 `/account-stats`：

- `GET /account-stats?id=<账号id>`        一个账号的粉丝 / 互动 / 作品数据
- `GET /account-stats/summary`            列表用：所有账号的粉丝数一览（只读缓存）

**为什么不挂在 `/xhs` 下**：和 `/session-renew` 是同一个理由——这份数据的对象是
「账号管理」里的**四个平台**，跟小红书无关。挂在 `/xhs` 前缀下会让人以为只有小红书
有数据（`myUtils/account_stats/` 里四个平台各有一个适配器）。

两个接口的分工
--------------
`/account-stats` 是**详情弹窗**用的：`refresh=1` 时真的去开浏览器抓，
可能几秒到十几秒。所以它永远先返回缓存（如果有），把"新不新鲜"交给前端判断。

`/account-stats/summary` 是**列表页**用的：它**只读缓存，绝不抓取**。
理由：列表页一进来就是 N 个账号，如果它也会触发抓取，打开账号管理页等于同时开
N 个浏览器；而且用户只是扫一眼列表，不该为这个付出一分钟的等待。
列表上要看到实时数字，就去点那一行的「详情」。

抓取是**阻塞**的（内部 `asyncio.run` + 浏览器），所以放在线程池里跑，
并且每个账号一把锁——见 `myUtils/account_stats/cache.py` 的 `account_lock()`。
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path

from flask import Blueprint, jsonify, request

from conf import BASE_DIR
from myUtils.account_stats import (
    AccountStatsError,
    cache,
    platform_name,
    platform_supported,
)
from myUtils.account_stats import sync as account_stats_sync

account_stats_bp = Blueprint("account_stats", __name__, url_prefix="/account-stats")

@account_stats_bp.get("/avatar/<int:account_id>")
def account_avatar(account_id: int):
    """返回**本地**存的账号头像（同步时下载下来的那张）。

    为什么不让前端直接引平台 CDN 的地址：热链不一定放行、URL 可能带时效，
    表现就是头像随机裂图。这里统一"同步时落地、列表读本地"。
    没有就 404——前端接到 404 / 图片加载失败时回退成原来的文字头像。
    """
    from flask import send_file

    from myUtils.account_stats.avatar import avatar_file

    path = avatar_file(account_id)
    if path is None:
        return _err("这个账号还没有本地头像（同步一次账号详情即可）", code=404)

    response = send_file(path)
    # 头像是"同步时更新"的，让浏览器缓存住、但同步后能立刻看到新的：
    # max-age 给 5 分钟，配合下面的 must-revalidate。
    response.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
    return response


#: 抓取用的线程池。
#:
#: 上限 2，而且**不共享** `browser_lock.BROWSER_SLOTS` 之外的额度：真正的并发上限
#: 由 `browser_lock` 统一管（默认同时 4 个浏览器），这里只是限制"同时有几个详情请求
#: 在等浏览器"。调大没有意义——瓶颈在浏览器名额，不在这里。
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="account-stats")

#: 详情默认最多带多少条作品。
#:
#: 20 条 ≈ 平台两页，界面上一屏能看到重点，请求也还在"点一下等几秒"的范围里。
MEDIA_LIMIT_DEFAULT = 20
MEDIA_LIMIT_MAX = 100

#: 单次抓取的**兜底上限**（秒）。比适配器自己的超时更宽：
#: 视频号 240 秒、其它平台 180 秒，所以取 260 保证正常情况下永远轮不到它生效。
#: 它防的是"适配器内部某一步真的挂住"，见下面 `future.result(timeout=...)` 的说明。
FETCH_RESULT_TIMEOUT = 260.0


def _ok(data=None, msg="ok"):
    return jsonify({"code": 200, "success": True, "msg": msg, "data": data})


def _err(msg: str, code: int = 500, data=None):
    return jsonify({"code": code, "success": False, "msg": str(msg), "data": data}), code


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(Path(BASE_DIR) / "db" / "database.db")
    conn.row_factory = sqlite3.Row
    return conn


def _load_account(account_id: int) -> dict | None:
    """按 id 取账号行。"""
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT id, type, filePath, userName, status FROM user_info WHERE id = ?",
                (account_id,),
            ).fetchone()
    except Exception as exc:
        raise AccountStatsError(f"读取账号信息失败：{exc}") from exc
    if row is None:
        return None
    return {
        "id": row["id"],
        "type": row["type"],
        "filePath": row["filePath"],
        "name": row["userName"],
        "status": row["status"],
    }


def _cache_envelope(
    record: dict | None,
    *,
    stale: bool,
    fetched: bool,
    error: str | None = None,
    unsupported: bool = False,
) -> dict:
    """把缓存行 + 本次抓取的结果拼成前端要的那一份。

    `stale` 和 `error` 是可以**同时存在**的：抓取失败了、但手上还有上次的数据，
    这时应该"照常显示旧数据 + 标一行红字说明为什么没更新"，而不是把弹窗清空。
    """
    stats = (record or {}).get("stats") or {}
    age = cache.age_seconds((record or {}).get("fetched_at"))
    return {
        "stats": stats or None,
        "fetched_at": (record or {}).get("fetched_at"),
        "age_seconds": age,
        "stale": bool(stale),
        "fetched": bool(fetched),
        "error": error,
        "unsupported": bool(unsupported),
    }


def _fetch_blocking(account: dict, with_media: bool, media_limit: int) -> dict:
    """在工作线程里抓一次并落库。异常和失败码一律翻译成 `(stats, error, unsupported)`。

    **落库这一步交给 `sync_account()`**（而不是在这里 `cache.save`）：抓取 + 覆盖本地库
    这件事现在有三个入口——续期定时任务、「同步」按钮、账号刚加进来——都走那一个函数。
    这里要的只是把它的结论翻译成详情接口用的形状，别再自己写一遍写库逻辑。
    """
    sync_row = account_stats_sync.sync_account(
        {
            "id": account["id"],
            "type": account["type"],
            "file_path": account["filePath"],
            "name": account["name"],
            "platform": platform_name(account["type"]),
        },
        with_media=with_media,
        media_limit=media_limit,
    )
    status = sync_row.get("status")
    message = sync_row.get("message")

    if status == account_stats_sync.STATUS_OK:
        # 从库里读回来，而不是让 sync_account 把整份 stats 也返回：
        # 返回一份能保证"界面看到的 == 库里存的"，不会出现两边不一致。
        record = cache.load(int(account["id"]))
        return {
            "error": None,
            "unsupported": False,
            "stats": (record or {}).get("stats"),
        }
    if status == account_stats_sync.STATUS_INVALID:
        return {"error": message, "unsupported": True, "stats": None}
    return {"error": message, "unsupported": False, "stats": None}


@account_stats_bp.get("")
@account_stats_bp.get("/")
def account_stats_detail():
    """一个账号的粉丝 / 互动 / 作品数据。

    查询参数：
    * `id`          账号 id（`user_info.id`），必填
    * `refresh`     `1` / `true` = 忽略缓存，真去抓一次（可能几秒到十几秒）
    * `media_limit` 作品条数上限，默认 20，最大 100
    * `with_media`  `0` = 不取作品列表（只要粉丝/互动数据时更快）
    """
    raw_id = (request.args.get("id") or "").strip()
    if not raw_id.isdigit():
        return _err("缺少或非法的 id 参数（应为账号 id）", 400)
    account_id = int(raw_id)

    try:
        with_media = (request.args.get("with_media") or "1").lower() not in ("0", "false", "no")
        media_limit = int(request.args.get("media_limit") or MEDIA_LIMIT_DEFAULT)
    except ValueError:
        return _err("media_limit 必须是数字", 400)
    media_limit = max(1, min(media_limit, MEDIA_LIMIT_MAX))

    force = (request.args.get("refresh") or "").lower() in ("1", "true", "yes")

    # 缓存要在**所有分支之前**读出来：下面"账号不存在"那条路也要用它（账号行没了、
    # 但缓存还留着的情况确实存在——比如手工改过库）。放在 `account is None` 之后读会
    # 直接抛 `UnboundLocalError`（这个 bug 是被 tests/test_account_stats.py 抓出来的）。
    cached = cache.load(account_id)

    try:
        account = _load_account(account_id)
    except AccountStatsError as exc:
        return _err(str(exc))
    if account is None:
        # 刻意**不用 404**：前端的 axios 拦截器把 404 统一弹成「请求地址不存在」，
        # 那句话在这里完全是误导（路径是对的，是账号没了）。走 200 + error 说明，
        # 弹窗里能显示一句人话。
        return _ok(
            _cache_envelope(
                cached,
                stale=False,
                fetched=False,
                error=f"账号不存在（id={account_id}），可能已经被删掉了。",
            )
        )

    # 平台没适配器：直接给一条说明，连缓存都不看（缓存里也不会有）。
    if not platform_supported(account["type"]):
        return _ok(
            _cache_envelope(
                cached,
                stale=False,
                fetched=False,
                error=f"{platform_name(account['type'])}暂时没有提供账号数据接口。",
                unsupported=True,
            )
        )

    if not force and cache.is_fresh(cached):
        return _ok(_cache_envelope(cached, stale=False, fetched=False))

    # 到这里就是真的要抓了。**先确保有旧数据可用**：抓取失败时把旧数据一起返回，
    # 前端能继续显示上次的数字（见 `_cache_envelope` 的说明）。
    lock = cache.account_lock(account_id)
    if not lock.acquire(blocking=False):
        # 已经有同账号的抓取在跑（用户连点两下刷新，或列表和详情同时请求）。
        # 不去排队——排队会让这次请求卡几十秒，而旧数据立刻就给得出来。
        return _ok(
            _cache_envelope(
                cached,
                stale=True,
                fetched=False,
                error="这个账号正在刷新数据，稍等一下再点「刷新」就能看到最新的。",
            )
        )
    try:
        future = _EXECUTOR.submit(_fetch_blocking, account, with_media, media_limit)
        try:
            # 适配器自己已经有超时（video 号 240 秒、其它 180 秒），这里再加一层**更宽**的
            # 兜底。理由：线城是杀不掉的，万一适配器里某一步真的挂住（浏览器收尾卡死之类），
            # 没有这一层的话这个请求会一直占着线程和那把账号锁，界面就是"转圈转到天荒地老"。
            # 宁可超时后带着明确原因失败，也不要无声地挂住。
            result = future.result(timeout=FETCH_RESULT_TIMEOUT)
        except FuturesTimeout:
            return _ok(
                _cache_envelope(
                    cached,
                    stale=True,
                    fetched=False,
                    error=(
                        f"读取{platform_name(account['type'])}数据超过了 "
                        f"{int(FETCH_RESULT_TIMEOUT)} 秒还没读完，已放弃。"
                        "可以稍后重试；如果每次都这样，多半是平台那边很慢或登录态出了问题。"
                    ),
                )
            )
    finally:
        lock.release()

    fresh_record = cache.load(account_id) if result.get("stats") else None
    return _ok(
        _cache_envelope(
            fresh_record or cached,
            # 抓取失败时旧数据就是"过期"的，标出来让界面显示"数据是 X 前的"
            stale=bool(result.get("error")) or not result.get("stats"),
            fetched=bool(result.get("stats")),
            error=result.get("error"),
            unsupported=bool(result.get("unsupported")),
        )
    )


@account_stats_bp.post("/sync")
def account_stats_sync_now():
    """**手动同步**一个账号：拉平台实时数据覆盖本地库（详情页的「同步」按钮）。

    和 `GET /account-stats?refresh=1` 的区别在**语义**，不在结果：

    * `refresh=1` 是"详情页打开时顺手更新一下"，还带着缓存优先、失败保留旧数据那一套；
    * `POST /account-stats/sync` 是**明确的同步动作**——用户点了就是要把本地库刷成最新的，
      所以它不看 TTL、必抓、必覆盖，并把"同步成没成功"作为一个**结论**回给界面
      （`ok` / `invalid` / `busy` / `error`），而不是只塞在 `error` 字段里。

    结果仍然按详情接口那套信封返回（`stats` + `stale` + `error`），
    这样前端同步完可以直接用同一段代码刷新弹窗，不用两套渲染逻辑。

    body（都可选）：`{"id": 1, "media_limit": 20, "with_media": true}`
    """
    body = request.get_json(silent=True) or {}
    raw_id = body.get("id", request.args.get("id"))
    try:
        account_id = int(raw_id)
    except (TypeError, ValueError):
        return _err("缺少或非法的 id（应为账号 id）", 400)

    try:
        media_limit = int(body.get("media_limit") or MEDIA_LIMIT_DEFAULT)
    except (TypeError, ValueError):
        return _err("media_limit 必须是数字", 400)
    media_limit = max(1, min(media_limit, MEDIA_LIMIT_MAX))
    with_media = body.get("with_media", True)
    if isinstance(with_media, str):
        with_media = with_media.lower() not in ("0", "false", "no")

    cached = cache.load(account_id)
    try:
        account = _load_account(account_id)
    except AccountStatsError as exc:
        return _err(str(exc))
    if account is None:
        return _ok(
            _cache_envelope(
                cached,
                stale=False,
                fetched=False,
                error=f"账号不存在（id={account_id}），可能已经被删掉了。",
            )
        )

    # 同一个账号同时只抓一次：用户可能连点两下「同步」，而连点会开出两个浏览器去读
    # 同一个账号——正是各平台风控最敏感的模式（同一个登录态短时间两次访问）。
    lock = cache.account_lock(account_id)
    if not lock.acquire(blocking=False):
        return _ok(
            _cache_envelope(
                cached,
                stale=True,
                fetched=False,
                error="这个账号正在同步中，稍等一下就能看到最新数据。",
            )
        )
    try:
        future = _EXECUTOR.submit(
            account_stats_sync.sync_account,
            {
                "id": account["id"],
                "type": account["type"],
                "file_path": account["filePath"],
                "name": account["name"],
                "platform": platform_name(account["type"]),
            },
            with_media=with_media,
            media_limit=media_limit,
        )
        try:
            result = future.result(timeout=FETCH_RESULT_TIMEOUT)
        except FuturesTimeout:
            result = {
                "status": account_stats_sync.STATUS_ERROR,
                "message": (
                    f"同步超过 {int(FETCH_RESULT_TIMEOUT)} 秒还没完成，已放弃。"
                    "可以稍后重试；如果每次都这样，多半是平台那边很慢或登录态出了问题。"
                ),
            }
    finally:
        lock.release()

    status = result.get("status")
    fresh_record = cache.load(account_id) if status == account_stats_sync.STATUS_OK else None
    envelope = _cache_envelope(
        fresh_record or cached,
        stale=status != account_stats_sync.STATUS_OK,
        fetched=status == account_stats_sync.STATUS_OK,
        error=None if status == account_stats_sync.STATUS_OK else result.get("message"),
        unsupported=status == account_stats_sync.STATUS_INVALID,
    )
    # 额外带上原始结论，前端据此决定提示的语气（成功是绿色 toast，拿不到数据是说明）
    envelope["sync"] = {
        "status": status,
        "message": result.get("message"),
        "synced_at": result.get("synced_at"),
    }
    return _ok(envelope)


@account_stats_bp.get("/summary")
def account_stats_summary():
    """列表用：所有账号的粉丝数一览。**只读缓存，绝不触发抓取。**

    返回 `{account_id: {fans_count, likes_total, fetched_at, age_seconds, stale, ...}}`。
    没抓过的账号不在结果里——前端据此显示「—」而不是 0。
    """
    try:
        with _db() as conn:
            rows = conn.execute("SELECT id, type, userName FROM user_info").fetchall()
    except Exception as exc:
        return _err(f"读取账号列表失败：{exc}")

    ids = [row["id"] for row in rows]
    records = cache.load_many(ids)

    out: dict[str, dict] = {}
    for row in rows:
        record = records.get(row["id"])
        if not record:
            continue
        stats = record.get("stats") or {}
        fans = stats.get("fans") or {}
        engagement = stats.get("engagement") or {}
        media = stats.get("media") or {}
        age = cache.age_seconds(record.get("fetched_at"))
        out[str(row["id"])] = {
            "fans_count": fans.get("count"),
            "following_count": fans.get("following_count"),
            "likes_total": fans.get("likes_total"),
            "views_7d": ((engagement.get("periods") or {}).get("7d") or {}).get("view_count"),
            "rise_fans_7d": ((engagement.get("periods") or {}).get("7d") or {}).get("rise_fans_count"),
            "media_count": len(media.get("items") or []) or None,
            "media_supported": bool(media.get("supported")),
            "fetched_at": record.get("fetched_at"),
            "age_seconds": age,
            "stale": (age is None) or (age >= cache.TTL_SECONDS),
            # 平台名也回一份，前端拼提示语（"小红书的数据是 10 分钟前的"）时不用另查
            "platform_name": platform_name(row["type"]),
        }
    return _ok(out)


def ensure_tables() -> None:
    """启动时建表（幂等）。转发到 cache，方便 `sau_backend.py` 一处调用。"""
    cache.ensure_tables()


__all__ = ["account_stats_bp", "ensure_tables"]
