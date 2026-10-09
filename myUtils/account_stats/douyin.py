# -*- coding: utf-8 -*-
"""抖音账号数据适配器（at-ai-xhs 改造新增）。

抓什么
------
| 数据 | 来源接口 | 对应界面 |
| --- | --- | --- |
| 粉丝数 / 关注数 / 获赞 / 抖音号 / 头像 | `GET /aweme/v1/creator/user/info/` | 账号概览卡片 |
| 作品数 | `GET /web/api/media/user/info/` | 账号概览卡片 |
| 近 7 / 30 天播放、点赞、评论、分享、增粉、掉粉（含逐日明细） | `GET /aweme/janus/creator/data/overview/all/?last_days_type=N` | 粉丝 + 互动页 |
| 每条作品的播放 / 点赞 / 收藏 / 评论 / 分享 / 吸粉 / 完播率 | `GET /web/api/creator/item/list` | 作品数据页 |

**这是四个平台里唯一一个不用开浏览器的**——实测这几个 creator 端点**不需要
`a_bogus` / `X-Bogus` / `msToken` 之类的签名**，裸 `requests` + cookie 直接 200
（连续 5 次调用 3 个核心端点，15/15 全 200，没有限流）。所以这里干脆用 HTTP 直连：
抓一次一两秒，比开浏览器快一个数量级，也不用担心浏览器 profile 被发布任务占住。

只有两个额外约束：
* 必须带 `Referer: https://creator.douyin.com/creator-micro/home` 和一个像真浏览器的
  `User-Agent`（不带 Referer 会被当成站外请求）；
* 响应里的**数字全是字符串**（`"view_count": "40"`），必须显式 `int()` / `float()`，
  不转的话前端会拿 `"188"` 去做加法变成拼接。

`last_days_type` 的取值实测确认：**1 = 近 7 日，2 = 近 15 日，3 = 近 30 日**。
（抖音的数据中心标注"每天 12 点更新"，所以当天数据可能还没有，这是平台的更新节奏，
不是抓取失败——界面上照实显示即可。）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

from conf import BASE_DIR

from .base import AccountStatsError, StatsUnavailable, build_stats

BASE_URL = "https://creator.douyin.com"

#: 单次 HTTP 请求超时（秒）。
_TIMEOUT = 25

#: 真正能拿到数据的请求头。
#:
#: `Referer` 是**必须的**：抖音的 creator 端点是站内接口，缺 Referer 会被当成跨站
#: 请求拒掉。UA 装成真浏览器是配套的（项目里小红书发布链路刻意不硬编码 UA 是因为
#: 有头模式下真实内核更准；这里是纯 HTTP，没有内核可借，只能给一个常见的 Chrome UA）。
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN",
    "Referer": "https://creator.douyin.com/creator-micro/home",
}

#: 作品列表每页条数。抖音这边 `count` 直接给 30 能一次拿完（实测 28 条全返回）。
_ITEM_PAGE_SIZE = 30

#: `overview/all` 的 `last_days_type` → 我们的周期 key。
_PERIOD_TYPES: dict[str, int] = {"7d": 1, "30d": 3}

#: `overview/all` 里每个指标的 key → 规范化字段名。
#:
#: 抖音把"增粉"和"掉粉"分开给（`new_fans` / `cancel_fans`），净增要自己减。
_OVERVIEW_FIELDS: dict[str, str] = {
    "play": "view_count",
    "digg": "like_count",
    "comment": "comment_count",
    "share": "share_count",
    "profile": "profile_view_count",
    "new_fans": "new_fans",
    "cancel_fans": "cancel_fans",
    "post_search": "search_view_count",
}


def _account_file_path(account_file: str) -> Path:
    return Path(BASE_DIR) / "cookiesFile" / str(account_file or "")


def _session(account_file: str, account_name: str) -> requests.Session:
    """用账号的 storage_state 建一个带 cookie 的 session。"""
    path = _account_file_path(account_file)
    if not path.is_file():
        raise StatsUnavailable(
            f"账号「{account_name}」的 cookie 文件不存在（{path.name}），需要先扫码登录。"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AccountStatsError(f"读 cookie 文件失败（{path.name}）：{exc}") from exc

    session = requests.Session()
    for cookie in raw.get("cookies") or []:
        if not isinstance(cookie, dict) or not cookie.get("name"):
            continue
        try:
            session.cookies.set(
                cookie["name"],
                cookie.get("value") or "",
                domain=str(cookie.get("domain") or "").lstrip("."),
                path=cookie.get("path") or "/",
            )
        except Exception:
            # 个别 cookie 的 domain 写法（例如带端口的）requests 会拒，跳过就好，
            # 少一条 cookie 不影响登录态判断——真失效了会在下面的接口上报出来。
            continue
    return session


def _get_json(session: requests.Session, path: str, what: str) -> dict[str, Any]:
    url = f"{BASE_URL}{path}"
    try:
        response = session.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise AccountStatsError(f"请求抖音{what}失败：{exc}") from exc

    if response.status_code in (401, 403):
        raise StatsUnavailable(
            f"抖音拒绝了{what}（{response.status_code}）。登录态可能已失效，"
            "可以试试「重新扫码」。"
        )
    if response.status_code != 200:
        raise AccountStatsError(f"抖音{what}返回 {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise AccountStatsError(f"抖音{what}返回的不是 JSON（{response.text[:120]}）") from exc
    if not isinstance(payload, dict):
        raise AccountStatsError(f"抖音{what}返回的结构不是对象")
    # 抖音的业务错误码：`status_code != 0` 就是失败（HTTP 依然是 200）。
    status = payload.get("status_code")
    if status not in (None, 0):
        msg = payload.get("status_msg") or "未知原因"
        raise AccountStatsError(f"抖音{what}返回异常（{status} {msg}）")
    return payload


def _int(value: Any) -> int | None:
    """抖音的数字都是字符串，统一转成 int；转不了就 None（**不要变成 0**）。"""
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _overview_period(data: dict[str, Any], period_key: str) -> dict[str, Any]:
    """`overview/all` 的 `data` → 规范化的一段（含逐日明细）。"""
    period: dict[str, Any] = {}
    series: dict[str, list[dict[str, Any]]] = {}

    for source_key, field in _OVERVIEW_FIELDS.items():
        block = data.get(source_key)
        if not isinstance(block, dict):
            continue
        count = _int(block.get("current_count"))
        if count is not None:
            period[field] = count
        trend: list[dict[str, Any]] = []
        for point in block.get("option_list") or []:
            if not isinstance(point, dict):
                continue
            date = point.get("date")
            value = _int(point.get("count"))
            if date and value is not None:
                trend.append({"date": str(date), "count": value})
        if trend:
            # 平台返回的顺序是**乱的**（实测 7 天明细里 9-14 排在 9-27 前面），
            # 画趋势图必须自己排一次。
            trend.sort(key=lambda row: row["date"])
            series[field] = trend

    # 净增粉丝 = 新增 − 取关。两个平台（小红书 / 快手）都直接给净增，抖音要自己算，
    # 但算出来的口径和它们一致，前端就能用同一套字段渲染。
    new_fans = period.get("new_fans")
    cancel_fans = period.get("cancel_fans")
    if new_fans is not None or cancel_fans is not None:
        period["rise_fans_count"] = new_fans
        period["loss_fans_count"] = cancel_fans
        period["net_rise_fans_count"] = (new_fans or 0) - (cancel_fans or 0)

    if series:
        # 净增的逐日明细也合出来（前端画"涨粉趋势"要的是它）
        new_series = {row["date"]: row["count"] for row in series.get("new_fans", [])}
        cancel_series = {row["date"]: row["count"] for row in series.get("cancel_fans", [])}
        if new_series or cancel_series:
            dates = sorted(set(new_series) | set(cancel_series))
            series["net_rise_fans_count"] = [
                {"date": d, "count": new_series.get(d, 0) - cancel_series.get(d, 0)} for d in dates
            ]
            series["rise_fans_count"] = [{"date": d, "count": new_series[d]} for d in sorted(new_series)]
            series["loss_fans_count"] = [
                {"date": d, "count": cancel_series[d]} for d in sorted(cancel_series)
            ]
        period["series"] = series

    # `new_fans` / `cancel_fans` 已经换算成规范化的 `rise_fans_count` /
    # `loss_fans_count` 了，这里把原始 key 删掉——留着会让前端看到两套名字，
    # 而它们的含义完全一样（小红书、快手都只给了规范化那一套）。
    period.pop("new_fans", None)
    period.pop("cancel_fans", None)
    period.pop("period", None)
    return period


def _item(row: dict[str, Any]) -> dict[str, Any]:
    """一条作品 → 规范化的一行。"""
    metrics = row.get("metrics") or {}
    video_info = row.get("video_info") or {}
    cover = row.get("cover") or {}
    cover_url = None
    for url in (cover.get("url_list") if isinstance(cover, dict) else None) or []:
        cover_url = url
        break

    item_id = row.get("id")
    duration_ms = _int(video_info.get("duration")) if isinstance(video_info, dict) else None
    completion = _float(metrics.get("completion_rate"))
    created = _int(row.get("create_time"))
    published_at = None
    if created:
        from datetime import datetime

        published_at = datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M")

    # 抖音的时间是**秒**，小红书/快手是毫秒；统一成"秒"给前端（前端按需展示）。
    return {
        "id": str(item_id) if item_id is not None else None,
        "title": (row.get("description") or "（无标题）").strip() or "（无标题）",
        "type": row.get("type") or "video",
        "cover": cover_url,
        "published_at": published_at,
        "duration": round(duration_ms / 1000) if duration_ms else None,
        "views": _int(metrics.get("view_count")),
        "likes": _int(metrics.get("like_count")),
        "collects": _int(metrics.get("favorite_count")),
        "comments": _int(metrics.get("comment_count")),
        "shares": _int(metrics.get("share_count")),
        # 这张作品带来了几个新粉丝（`subscribe_count` 就是抖音的"吸粉量"）
        "rise_fans": _int(metrics.get("subscribe_count")),
        "full_view_rate": round(completion * 100, 2) if completion is not None else None,
        "url": f"https://www.douyin.com/video/{item_id}" if item_id else None,
    }


def _collect_items(session: requests.Session, media_limit: int) -> dict[str, Any]:
    """翻页取作品列表，返回规范化后的 `media` 段。"""
    import time as _time

    now_ms = int(_time.time() * 1000)
    collected: list[dict[str, Any]] = []
    total: int | None = None
    max_cursor = 0

    # 翻页上限：正常一次就够（`count=30` 实测能一次拿完全部 28 条），这个上限只是
    # 防止"平台一直说 has_more"导致我们无限请求——宁可少几条，不能转圈。
    for _ in range(4):
        if len(collected) >= media_limit:
            break
        page_payload = _get_json(
            session,
            "/web/api/creator/item/list"
            f"?count={_ITEM_PAGE_SIZE}&fields=visibility,metrics"
            f"&start_time=0&end_time={now_ms}"
            f"&need_long_article=true&max_cursor={max_cursor}",
            "作品列表",
        )
        rows = page_payload.get("items") or []
        if total is None:
            total = _int(page_payload.get("total"))
        if not rows:
            break
        seen = {row.get("id") for row in collected if row.get("id")}
        fresh = [
            row for row in rows if isinstance(row, dict) and str(row.get("id")) not in seen
        ]
        if not fresh:
            # 游标没往前走（还在返回同一页），继续请求只会重复拿同一批数据。
            break
        collected.extend(_item(row) for row in fresh)
        if not page_payload.get("has_more"):
            break
        next_cursor = _int(page_payload.get("max_cursor"))
        if next_cursor is None or next_cursor == max_cursor:
            break
        max_cursor = next_cursor

    items = collected[:media_limit]
    return {
        "supported": True,
        "items": items,
        "total": total if total is not None else len(items),
        "has_more": bool(total and total > len(items)),
    }


def fetch(
    account_file: str,
    account_name: str,
    *,
    with_media: bool = True,
    media_limit: int = 20,
    wait_timeout: float = 0.0,  # 纯 HTTP，不开浏览器，不需要等锁（仅为签名对齐）
) -> dict[str, Any]:
    """抓一个抖音账号的数据（**阻塞**，必须在工作线程里调用）。

    全程 HTTP，不开浏览器——所以这里**不用 `browser_session` 占浏览器名额**：
    它跟发布任务不抢设备，也就不该排在发布后面等锁。
    """
    session = _session(account_file, account_name)
    notes: list[str] = []

    # 账号概览和两个周期总览是**三个互不依赖的请求**，串着发要等三轮（实测下来
    # 光这段就二十多秒，用户点一下详情会以为卡住了）。并发发出去，总耗时按最慢的那个算。
    # 作品列表依赖不了它们、但会跑很多次翻页，所以也一起丢进池子。
    from concurrent.futures import ThreadPoolExecutor

    def _overview(period_key: str, days_type: int) -> dict[str, Any]:
        payload = _get_json(
            session,
            f"/aweme/janus/creator/data/overview/all/?last_days_type={days_type}",
            f"近 {period_key} 数据总览",
        )
        return _overview_period(payload.get("data") or {}, period_key)

    def _media_user() -> dict[str, Any]:
        return _get_json(session, "/web/api/media/user/info/", "账号统计").get("user") or {}

    def _media_items() -> dict[str, Any]:
        return _collect_items(session, media_limit)

    with ThreadPoolExecutor(max_workers=4) as pool:
        user_future = pool.submit(_get_json, session, "/aweme/v1/creator/user/info/", "账号信息")
        media_user_future = pool.submit(_media_user)
        overview_futures = {
            key: pool.submit(_overview, key, days_type)
            for key, days_type in _PERIOD_TYPES.items()
        }
        media_future = pool.submit(_media_items) if with_media else None

        payload = user_future.result()
        verify = payload.get("douyin_user_verify_info") or {}
        if not verify:
            raise StatsUnavailable(
                f"抖音没有返回账号信息（账号「{account_name}」）。登录态可能已失效。"
            )

        # 作品数等补充信息在另一个接口上。它挂了不影响主体，所以单独兜住。
        media_user: dict[str, Any] = {}
        try:
            media_user = media_user_future.result()
        except (AccountStatsError, StatsUnavailable) as exc:
            notes.append(f"抖音的账号统计（作品数等）没取到：{exc}")

        periods: dict[str, Any] = {}
        for period_key, future in overview_futures.items():
            try:
                block = future.result()
            except (AccountStatsError, StatsUnavailable) as exc:
                notes.append(f"抖音{period_key}的数据总览没取到：{exc}")
                continue
            if block:
                periods[period_key] = block

        media: dict[str, Any]
        if media_future is not None:
            try:
                media = media_future.result()
            except (AccountStatsError, StatsUnavailable) as exc:
                media = {"supported": False, "items": [], "total": None, "has_more": False}
                media["error"] = str(exc)
                notes.append(f"作品列表没有取到：{exc}")
        else:
            media = {"supported": False, "items": [], "total": None, "has_more": False}
            notes.append("这次只取了账号与粉丝数据，没有取作品列表。")

    fans: dict[str, Any] = {
        "count": _int(verify.get("follower_count")),
        "following_count": _int(verify.get("following_count")),
        "likes_total": _int(verify.get("total_favorited")),
        "periods": {},
    }
    for period_key, block in periods.items():
        fans["periods"][period_key] = {
            "gained": block.get("new_fans"),
            "lost": block.get("cancel_fans"),
            "net": block.get("net_rise_fans_count"),
            "rise_series": (block.get("series") or {}).get("rise_fans_count") or [],
            "loss_series": (block.get("series") or {}).get("cancel_fans") or [],
            "net_series": (block.get("series") or {}).get("net_rise_fans_count") or [],
        }

    engagement: dict[str, Any] = {"periods": periods}
    engagement["lifetime"] = {
        "likes_and_collects": _int(verify.get("total_favorited")),
        "fans": _int(verify.get("follower_count")),
        "following": _int(verify.get("following_count")),
    }

    return build_stats(
        platform_no=3,
        account_name=account_name,
        nickname=verify.get("nick_name") or media_user.get("nickname"),
        avatar=verify.get("avatar_url"),
        account_id=str(verify.get("douyin_unique_id") or "") or None,
        fans=fans,
        engagement=engagement,
        media=media,
        extra={
            "aweme_count": _int(media_user.get("aweme_count")),
            "favoriting_count": _int(media_user.get("favoriting_count")),
        },
        notes=notes,
    )


__all__ = ["fetch"]
