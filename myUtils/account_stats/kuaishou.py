# -*- coding: utf-8 -*-
"""快手账号数据适配器（at-ai-xhs 改造新增）。

抓什么
------
| 数据 | 来源接口 | 对应界面 |
| --- | --- | --- |
| 粉丝数 / 头像 / 用户 id | `/rest/cp/creator/pc/home/userInfo` | 账号概览卡片 |
| 获赞总数 / 关注数 / 简介 | `/rest/cp/creator/pc/home/infoV2` | 账号概览卡片 |
| 近 7 天播放 / 点赞 / 涨粉 / 评论 / 分享 / 完播率（含逐日趋势） | `/rest/cp/creator/analysis/pc/home/author/overview` | 粉丝 + 互动页 |
| 每条作品的播放 / 点赞 / 收藏 / 评论 / 涨粉 | `/rest/cp/creator/analysis/pc/home/photo/list` | 作品数据页 |

为什么是"进页面顺手捡响应"而不是"自己发请求"
--------------------------------------------
快手的 cp 平台把签名放在 **query 参数 `__NS_sig3`** 上，而且**只有一部分路由校验它**
（实测：`author/overview` 给个乱写的 sig3 也返回正常数据，`userInfo` / `infoV2` /
作品列表则必须有真签名）。更麻烦的是：

* **真签名也不能搬到 `requests` 里用**。实测把页面发出的那条带最新 sig3 的 URL
  原样重放（连浏览器的实时 cookie 一起带上）依然是 `{"result":500002,"message":"请稍后重试"}`；
  同一条 URL 在 `page.evaluate(fetch)` 里就正常。也就是说它绑的是**浏览器请求上下文**
  （TLS/HTTP2 指纹或客户端里的某个值），不是 cookie + URL 能复现的东西。
* 所以"用 requests 直连"这条路对快手是**走不通的**，只能真的开一个页面。

于是这里的做法是：**打开 `cp.kuaishou.com/profile`，让平台自己的 JS 去发这些请求，
我们只负责把响应收下来**。好处很实在：

1. 不用逆 `__NS_sig3`（那是签名算法，会随平台更新而失效）；
2. 一次页面加载就把五个接口全触发了（实测 `userInfo` / `infoV2` / 作品列表 /
   `author/overview` 全都在首屏就发出去），不用逐个构造请求；
3. 拿到的就是页面自己要用的数据，字段结构不会因为我们拼错参数而变形。

代价是**依赖页面的加载顺序**：平台哪天不再首屏拉某个接口，这里就拿不到了。
所以每个数据块都是**各自独立地尽力而为**——某个接口没截到，只影响它对应的那一段
（并在 `notes` 里说明），不会让整个详情页变成空白。

已知拿不到的数据
----------------
* **单条作品的分享数**：翻遍所有能截到的响应都没有这个字段（快手只在账号级
  `SHARE` 里给总量）。所以作品表里快手那一行**不显示分享列**，而不是显示 0。
* **30 天 / 90 天区间**：`author/overview` 的 `timeType` 只认 1（实测传 30 / 90 都是
  `result: 411`）。所以快手的 `periods` 里**只有 `7d`**，前端按"有哪些 tab"渲染。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from conf import BASE_DIR

from .base import AccountStatsError, StatsUnavailable, build_stats

#: 页面加载 + 等那五个接口响应回来的上限（秒）。实测首屏 6~9 秒。
_CAPTURE_TIMEOUT = 60.0

#: 整个抓取流程的上限（秒）。
_FETCH_TIMEOUT = 180.0

#: 要截的接口。值是**URL 里的特征片段**（接口路径可能带 `?__NS_sig3=`，用精确匹配会漏）。
_WANTED: dict[str, str] = {
    "/rest/cp/creator/pc/home/userInfo": "user_info",
    "/rest/cp/creator/pc/home/infoV2": "info_v2",
    "/rest/cp/creator/analysis/pc/home/author/overview": "overview",
    "/rest/cp/creator/analysis/pc/home/photo/list": "photo_list",
}

#: 作品列表每页条数。平台自己用 15，这里跟着用，少一次请求。
_PHOTO_PAGE_SIZE = 15

#: 账号级 `tab` → 规范化字段名。
#: `PURE_INCREASE_FAN` 是**净增粉丝**（快手自己的口径），`COMPLETE_RATIO` 是完播率（0~1 的小数）。
_OVERVIEW_TABS: dict[str, str] = {
    "PLAY": "view_count",
    "LIKE": "like_count",
    "COMMENT": "comment_count",
    "SHARE": "share_count",
    "PURE_INCREASE_FAN": "net_rise_fans_count",
    "COMPLETE_RATIO": "video_full_view_rate",
}

#: 从页面里现取 `kuaishou.web.cp.api_ph`。
#:
#: 这个值就是 cookie 里的原样，但**在页面里读比在 Python 里读更可靠**：cookie 可能
#: 在页面加载过程中被平台刷过，而以 Python 手上那份 storage_state 为准的话，
#: 用旧值发请求会被判成异常（实测 `infoV2` 缺这个字段直接 500002）。
_API_PH_JS = """
() => {
  const m = document.cookie.match(/(?:^|;\\s*)kuaishou\\.web\\.cp\\.api_ph=([^;]*)/);
  return m ? decodeURIComponent(m[1]) : '';
}
"""

#: 页面内发一个 POST。只用于**页面自己已经发过的那几条**接口补窗口/翻页——
#: 页面里发请求能复用它的请求上下文，`requests` 不行（原因见模块 docstring）。
_POST_JS = r"""
async ([path, body]) => {
  try {
    const r = await fetch(path, {
      method: 'POST',
      credentials: 'include',
      headers: {'Content-Type': 'application/json;charset=UTF-8'},
      body: JSON.stringify(body),
    });
    return {ok: true, status: r.status, body: await r.text()};
  } catch (e) {
    return {ok: false, error: String(e)};
  }
}
"""


def _account_file_path(account_file: str) -> Path:
    return Path(BASE_DIR) / "cookiesFile" / str(account_file or "")


def _fmt_date(raw: Any) -> str | None:
    """快手的日期是 `"20260925"` 这种紧凑格式 → `"2026-09-25"`。"""
    text = str(raw or "")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return None


def _ms_to_str(ms: Any) -> str | None:
    try:
        from datetime import datetime

        return datetime.fromtimestamp(float(ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


def _check_result(payload: dict[str, Any], what: str) -> dict[str, Any]:
    """快手**永远返回 HTTP 200**，失败藏在 body 的 `result` 里（实测 500002 / 411）。

    所以判成败必须看 `result`，看状态码会一路"成功"到后面的 KeyError。
    """
    if not isinstance(payload, dict):
        raise AccountStatsError(f"快手的{what}返回的不是对象")
    result = payload.get("result")
    if result != 1:
        msg = payload.get("message") or payload.get("error_msg") or "未知原因"
        if result in (411, 10001, 500002):
            # 411 = 参数不对；500002 = 签名/风控没过。两者都是"这条路现在走不通"。
            raise StatsUnavailable(f"快手{what}暂时读不到（{result} {msg}）")
        raise AccountStatsError(f"快手{what}返回异常：{result} {msg}")
    return payload.get("data") or {}


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #


async def _capture(page, timeout: float) -> dict[str, dict[str, Any]]:
    """打开个人主页，把 `_WANTED` 里那几个接口的响应收下来。

    用"等这几个 future"而不是"睡 N 秒再看"：既快（首个响应回来就开始处理）
    又稳（慢网下不会因为睡少了而漏收）。
    """
    loop = asyncio.get_event_loop()
    futures: dict[str, asyncio.Future] = {
        name: loop.create_future() for name in _WANTED.values()
    }

    async def on_response(response):
        url = response.url
        for fragment, name in _WANTED.items():
            if fragment in url:
                future = futures[name]
                # 同一个接口页面可能发**多次**（首屏 + 路由切换）。这里只收第一次，
                # 并且必须在 `set_result` 前判 `done()`——否则第二次会抛
                # `InvalidStateError: invalid state`，而它在 task 里没人接，
                # 只会变成一条吓人的 "Task exception was never retrieved" 日志。
                if future.done():
                    return
                try:
                    body = await response.text()
                except Exception:
                    return
                if not future.done():
                    future.set_result({"url": url, "body": body})
                return

    def handler(response):
        # `page.on` 的回调是同步接口，得自己把协程丢到事件循环上。
        # 这些 task 必须**留住引用**（见 `_TASKS`），否则可能被 GC 提前回收，
        # 变成 "Task was destroyed but it is pending" 这种难查的告警。
        task = asyncio.ensure_future(on_response(response))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)

    page.on("response", handler)

    try:
        await page.goto(
            "https://cp.kuaishou.com/profile", wait_until="domcontentloaded", timeout=45000
        )
    except Exception as exc:
        # **打不开页面 ≠ 抓取出错**：`net::ERR_CONNECTION_CLOSED` / 超时这类是网络瞬断
        # （发布中心同时在跑、机器被压住时最常见），账号和登录态都是好的。
        # 按 `pdd.py` 里写明的约定，这种情况归 `StatsUnavailable`——弹窗提示"这次没拿到、
        # 显示的是上次的数据"，而不是甩一条红色报错让人以为账号坏了。
        raise StatsUnavailable(f"打不开快手创作者服务平台：{exc}") from exc

    # 等全部到齐；缺的那些**不算失败**，交给调用方按"哪段没有"处理。
    await asyncio.wait(futures.values(), timeout=timeout)

    captured: dict[str, dict[str, Any]] = {}
    for name, future in futures.items():
        if future.done() and not future.cancelled() and future.exception() is None:
            captured[name] = future.result()
    return captured


#: 持有页面事件回调里创建的 task，避免被 GC 提前回收（Python 会警告 "Task was destroyed"）。
_TASKS: set[asyncio.Task] = set()


def _parse(captured: dict[str, dict[str, Any]], name: str) -> dict[str, Any] | None:
    """取某个接口的 `data`。没截到 / 解析不了就返回 None（**不抛异常**）。"""
    item = captured.get(name)
    if not item:
        return None
    try:
        payload = json.loads(item.get("body") or "")
        return _check_result(payload, name)
    except (AccountStatsError, StatsUnavailable, ValueError):
        return None


def _overview_period(data: dict[str, Any]) -> dict[str, Any]:
    """账号级 7 天总览 → `engagement.periods["7d"]` + 逐日趋势。

    快手把逐日明细直接挂在每个指标下面（`trendData`），比小红书那种"另开一个
    `xxx_list` 字段"省事，但对齐口径时要注意：**`sumCount` 是区间汇总，
    `endDayCount` 是"昨天"那一个点**，两者都不是明细的和。所以数值用 `sumCount`，
    明细照原样给趋势图用（不去拿它反算总量，免得和平台显示的数字对不上）。
    """
    blocks = data.get("basicData") or []
    if not isinstance(blocks, list):
        return {}
    period: dict[str, Any] = {}
    series: dict[str, list[dict[str, Any]]] = {}
    for block in blocks:
        if not isinstance(block, dict):
            continue
        field = _OVERVIEW_TABS.get(str(block.get("tab") or ""))
        if not field:
            continue
        is_rate = field == "video_full_view_rate"
        if block.get("sumCount") is not None:
            value = block["sumCount"]
            # 完播率是 0~1 的小数，统一换算成百分数——小红书的同名字段就是百分数，
            # 两个平台都进同一张表，口径不一致会让用户以为快手完播率是 0。
            if is_rate:
                try:
                    value = round(float(value) * 100, 2)
                except (TypeError, ValueError):
                    pass
            period[field] = value
        trend = []
        for point in block.get("trendData") or []:
            if not isinstance(point, dict):
                continue
            date = _fmt_date(point.get("date"))
            count = point.get("count")
            if date and count is not None:
                if is_rate:
                    try:
                        count = round(float(count) * 100, 2)
                    except (TypeError, ValueError):
                        pass
                trend.append({"date": date, "count": count})
        if trend:
            trend.sort(key=lambda row: row["date"])
            series[field] = trend
    if series:
        period["series"] = series
    return period


def _photo_item(item: dict[str, Any]) -> dict[str, Any]:
    """一条作品 → 规范化的一行。

    注意**没有 `shares`**：快手的作品级接口不提供分享数（见模块 docstring）。
    前端据此不显示这一列。
    """
    work_id = item.get("photoId") or item.get("workId")
    return {
        "id": work_id,
        "title": (item.get("title") or "（无标题）").strip(),
        "type": "video",
        "cover": item.get("cover") or item.get("publishCoverUrl"),
        "published_at": _ms_to_str(item.get("publishTime")),
        "duration": item.get("duration"),
        "views": item.get("playCount"),
        "likes": item.get("likeCount"),
        "collects": item.get("collectCount"),
        "comments": item.get("commentCount"),
        # 快手把"这条作品带来了几个新粉丝"直接给出来了，小红书的作品列表没有。
        "rise_fans": item.get("followCount"),
        # 完播率（0~1）→ 百分数
        "full_view_rate": (
            round(float(item["fpr"]) * 100, 2) if isinstance(item.get("fpr"), (int, float)) else None
        ),
        "url": f"https://www.kuaishou.com/short-video/{work_id}" if work_id else None,
    }


async def _collect_photos(page, api_ph: str, limit: int) -> tuple[list[dict], int | None, str | None]:
    """翻页取作品列表。返回 `(items, total, error)`。

    列表接口**没有 sig3 也认**（实测给乱写的值也返回正常数据），所以这里直接
    在页面里发 POST 翻页，不用像首屏那样靠截响应。
    """
    items: list[dict[str, Any]] = []
    total: int | None = None
    page_index = 0
    while len(items) < limit:
        result = await page.evaluate(
            _POST_JS,
            [
                "/rest/cp/creator/analysis/pc/home/photo/list",
                {"page": page_index, "count": _PHOTO_PAGE_SIZE, "kuaishou.web.cp.api_ph": api_ph},
            ],
        )
        if not result.get("ok"):
            return items, total, f"作品列表请求失败：{result.get('error')}"
        try:
            data = _check_result(json.loads(result["body"]), "作品列表")
        except (AccountStatsError, StatsUnavailable, ValueError) as exc:
            return items, total, str(exc) if items else f"作品列表读不到：{exc}"

        photo_list = data.get("photoList") or {}
        batch = photo_list.get("photoItems") or []
        if total is None and photo_list.get("totalCount") is not None:
            total = int(photo_list["totalCount"])
        if not batch:
            break
        items.extend(_photo_item(x) for x in batch if isinstance(x, dict))
        if len(batch) < _PHOTO_PAGE_SIZE:
            break
        page_index += 1
    return items[:limit], total, None


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #


async def _fetch_async(
    account_file: str,
    account_name: str,
    *,
    with_media: bool,
    media_limit: int,
) -> dict[str, Any]:
    from playwright.async_api import async_playwright

    from conf import LOCAL_CHROME_HEADLESS, XHS_LOCALE, XHS_TIMEZONE, XHS_VIEWPORT

    cookie_path = _account_file_path(account_file)
    if not cookie_path.is_file():
        raise StatsUnavailable(
            f"账号「{account_name}」的 cookie 文件不存在（{cookie_path.name}），需要先扫码登录。"
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=LOCAL_CHROME_HEADLESS)
        try:
            context = await browser.new_context(
                storage_state=str(cookie_path),
                locale=XHS_LOCALE or "zh-CN",
                timezone_id=XHS_TIMEZONE or "Asia/Shanghai",
                viewport=XHS_VIEWPORT or {"width": 1440, "height": 900},
            )
            try:
                page = await context.new_page()
                captured = await _capture(page, _CAPTURE_TIMEOUT)

                notes: list[str] = []
                user_info = _parse(captured, "user_info")
                info_v2 = _parse(captured, "info_v2")
                overview = _parse(captured, "overview")

                if user_info is None and info_v2 is None:
                    raise StatsUnavailable(
                        f"快手没有返回账号信息（账号「{account_name}」）。"
                        "登录态可能已失效，可以试试「重新扫码」。"
                    )
                for key, label in (
                    ("user_info", "账号信息"),
                    ("info_v2", "获赞与关注数"),
                    ("overview", "近 7 天数据总览"),
                ):
                    if _parse(captured, key) is None:
                        notes.append(f"快手的{label}这次没有取到（页面没有加载对应接口）。")

                core = (user_info or {}).get("coreUserInfo") or {}
                ph = ""
                try:
                    ph = await page.evaluate(_API_PH_JS) or ""
                except Exception:
                    pass

                media: dict[str, Any] = {
                    "supported": True,
                    "items": [],
                    "total": None,
                    "has_more": False,
                }
                if with_media:
                    items, total, error = await _collect_photos(page, ph, media_limit)
                    media["items"] = items
                    media["total"] = total
                    # 平台总数比我们取回来的多 → 说明还有下一页没取，让前端提示"只显示最近 N 条"
                    media["has_more"] = bool(total and total > len(items))
                    media["share_metric_supported"] = False
                    if error:
                        notes.append(error)
                        if not items:
                            media["supported"] = False
                            media["error"] = error
                else:
                    media["supported"] = False
                    notes.append("这次只取了账号与粉丝数据，没有取作品列表。")

                overview_period = _overview_period(overview or {})
                if overview_period:
                    notes.append("快手只提供近 7 天的区间数据（平台没有 30 天口径）。")
            finally:
                await context.close()
        finally:
            await browser.close()

    fans: dict[str, Any] = {
        "count": core.get("fansNum") if core.get("fansNum") is not None else (info_v2 or {}).get("fansCnt"),
        "following_count": (info_v2 or {}).get("followCnt"),
        "likes_total": (info_v2 or {}).get("likeCnt"),
        "periods": {},
    }
    if overview_period:
        net_series = (overview_period.get("series") or {}).get("net_rise_fans_count") or []
        fans["periods"]["7d"] = {
            "net": overview_period.get("net_rise_fans_count"),
            "net_series": net_series,
        }

    return build_stats(
        platform_no=4,
        account_name=account_name,
        nickname=core.get("userName") or (info_v2 or {}).get("userName"),
        avatar=core.get("headUrl"),
        account_id=str(core.get("userId") or (info_v2 or {}).get("userId") or "") or None,
        fans=fans,
        engagement={
            "periods": {"7d": overview_period} if overview_period else {},
            "lifetime": {
                "likes_and_collects": (info_v2 or {}).get("likeCnt"),
                "fans": core.get("fansNum"),
                "following": (info_v2 or {}).get("followCnt"),
            },
        },
        media=media,
        extra={
            "desc": (info_v2 or {}).get("desc"),
            "kwai_id": (info_v2 or {}).get("userKwaiId"),
            # 平台给的"超过 X% 同粉丝量创作者"，是很有用的定位信息，单独拎出来
            "percentile": {
                block.get("tab"): block.get("interpretDesc")
                for block in ((overview or {}).get("basicData") or [])
                if isinstance(block, dict) and block.get("interpretDesc")
            },
        },
        notes=notes,
    )


def fetch(
    account_file: str,
    account_name: str,
    *,
    with_media: bool = True,
    media_limit: int = 20,
    wait_timeout: float = 20.0,
) -> dict[str, Any]:
    """抓一个快手账号的数据（**阻塞**，必须在工作线程里调用）。"""
    from myUtils.browser_lock import browser_session

    with browser_session(_account_file_path(account_file), wait_timeout=wait_timeout):
        return asyncio.run(
            asyncio.wait_for(
                _fetch_async(
                    account_file,
                    account_name,
                    with_media=with_media,
                    media_limit=media_limit,
                ),
                timeout=_FETCH_TIMEOUT,
            )
        )


__all__ = ["fetch"]
