# -*- coding: utf-8 -*-
"""拼多多视频账号数据适配器（at-ai-xhs 改造新增）。

为什么这么做（和快手同一条路）
------------------------------
拼多多**没有**可直连的数据接口：创作者后台的所有 `/api/backbone/*`、`/sprite/obs/*`
请求都带页面现算的签名（上传还额外带 `anti-content`），搬到 `requests` 里会被拒。
所以照搬快手那套已经验证过的做法：

    **开一个浏览器进创作者后台，让页面自己去发那些请求，我们只把响应收下来解析。**

这样签名由页面自己算、cookie 由浏览器自己维护，平台改版时最多是接口路径变一变。
（发布链路也是同一个思路，见 `uploader/pdd_uploader/main.py`。）

数据从哪来（2026-10-09 用真实账号逐个核对过）
---------------------------------------------
| 接口 | 页面 | 拿到什么 |
| --- | --- | --- |
| `/sprite/obs/anchor/info` | 发布页 `video/home` | 昵称、头像、`anchorUid`、**粉丝数**（`fansNumDataTip`）、直播间 ID |
| `/api/backbone/anchor/query` | 发布页 | 昵称、头像、`anchorUid`、`roomId`（和上面互为兜底） |
| `/api/backbone/video/statistic/view` | 数据总览 `video-all-data` | 账号级总览（视频发布量/播放量/点赞/分享/评论/粉丝净增值）+ 每个指标的逐日序列与 7 天、30 天增量 |
| `/api/backbone/video/query` | 作品管理 `video-manage` | 作品列表（`feedId`/描述/封面/播放 `vv`/点赞/评论/分享/发布时间）+ `total` |

口径说明（如实呈现，不替平台美化）：

* 粉丝数平台给的是**字符串**（`"1"`），这里转成 int；
* 作品级**播放量**取 `vv`（拼多多的口径，新发布的作品常见是 0）；
* 作品级**分享数** `shares` 平台经常给 `null` —— 拿不到就不写到行里，
  前端按"平台没给"处理，而不是显示 0（和快手那条同一个原则）；
* `daysStatistic` 的逐日序列是**近 31 天**，7 天/30 天增量平台直接给（`sevenDaysAdd` / `oneMonthAdd`）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from conf import BASE_DIR
from conf import PDD_HEADLESS
from myUtils.account_stats.base import AccountStatsError
from myUtils.account_stats.base import StatsUnavailable
from myUtils.account_stats.base import build_stats
from myUtils.browser_lock import account_lock
from myUtils.browser_lock import browser_slot

#: 账号信息页（发布页）：`anchor/info` 与 `anchor/query` 都在这里发出来。
_INFO_URL = "https://live.pinduoduo.com/n-creator/video/home"
#: 数据总览页：`statistic/view` 在这里发出来。
_OVERVIEW_URL = "https://live.pinduoduo.com/n-creator/video/video-all-data"
#: 作品管理页：`video/query` 在这里发出来（带分页参数）。
_MANAGE_URL = "https://live.pinduoduo.com/n-creator/video/video-manage"

#: 想截的接口（URL 里的特征片段 → 便于后续按 key 取）。
_WANTED: dict[str, str] = {
    "anchorInfo": "/sprite/obs/anchor/info",
    "anchorQuery": "/api/backbone/anchor/query",
    "statistic": "/api/backbone/video/statistic/view",
    "feeds": "/api/backbone/video/query",
}

#: 单个页面加载 + 等接口回响的上限（秒）。实测三个页面各 6~10 秒。
_PAGE_TIMEOUT = 45.0
#: 整条抓取流程的上限（秒）。
_FETCH_TIMEOUT = 180.0

#: `overallStatistic` 里的中文标题 → 规范化字段名。
_OVERALL_FIELDS: dict[str, str] = {
    "视频发布量": "published",
    "视频播放量": "views",
    "视频点赞数": "likes",
    "视频分享量": "shares",
    "视频评论数": "comments",
    "粉丝净增值": "net_rise_fans",
}

#: `daysStatistic` 里的中文标题 → 规范化字段名。
_DAILY_FIELDS: dict[str, str] = {
    "播放量": "views",
    "视频点赞": "likes",
}


def _account_path(account_file: str) -> Path:
    """账号文件：`cookiesFile/` 下的文件名（和发布链路共用同一个 cookie）。"""
    return Path(BASE_DIR) / "cookiesFile" / account_file


def _to_int(value: Any) -> int | None:
    """平台爱把数字包成字符串（`"1"` / `"1粉丝"`），能转就转，转不了返回 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    digits = ""
    for char in text:
        if char.isdigit():
            digits += char
        elif digits:
            break
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _ms_to_str(value: Any) -> str | None:
    """发布时间是毫秒时间戳 → `YYYY-MM-DD HH:MM`。"""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return None


def _result_of(body: Any) -> dict[str, Any] | None:
    """取统一信封里的 `result`。

    拼多多的信封是 `{success, errorCode, errorMsg, result}`，**成功时 errorCode 也是非空的**
    （1000000），所以这里只认 `success` 和 `errorMsg`，不看 errorCode——
    发布链路就因为把 errorCode 当错误吃过一次亏（见 docs 的 1.9 节）。
    """
    if not isinstance(body, dict):
        return None
    if body.get("success") is False:
        return None
    result = body.get("result")
    return result if isinstance(result, dict) else None


async def _capture(page, url: str, wanted: dict[str, str], timeout: float) -> dict[str, Any]:
    """打开页面，把 `wanted` 里那几个接口的响应体收下来。

    做法和快手一样：**不自己发请求**（签名是页面现算的），只旁听页面自己的 XHR。
    每个接口只留第一条响应（同一个接口可能被页面调多次，首屏那条就够了）。
    """
    captured: dict[str, Any] = {}
    loop = asyncio.get_running_loop()
    pending: list[asyncio.Task] = []

    async def _on_response(response) -> None:
        try:
            url_key = response.url.split("?")[0]
            for name, marker in wanted.items():
                if name in captured or marker not in url_key:
                    continue
                try:
                    captured[name] = await response.json()
                except Exception:  # noqa: BLE001 —— 非 JSON 的响应直接跳过
                    continue
        except Exception:  # noqa: BLE001
            pass

    def _handler(response) -> None:
        # 事件回调是同步的：把异步解析丢成 task，并留个引用防止被 GC 回收
        task = loop.create_task(_on_response(response))
        pending.append(task)

    page.on("response", _handler)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if len(captured) >= len(wanted):
                break
            await asyncio.sleep(0.8)
    finally:
        try:
            page.remove_listener("response", _handler)
        except Exception:  # noqa: BLE001
            pass
    return captured


def _feed_item(feed: dict[str, Any]) -> dict[str, Any]:
    """一条作品 → 规范化的一行。

    **不写"平台没给"的字段**：拼多多的作品级 `shares` 经常是 `null`，
    硬写成 0 会让人以为"这条作品真的一条都没分享"（快手那条同一个取舍）。
    """
    feed_id = feed.get("feedId")
    item: dict[str, Any] = {
        "id": str(feed_id) if feed_id else None,
        "title": (feed.get("title") or feed.get("desc") or "（无标题）").strip().split("\n")[0][:40],
        "type": "video",
        "cover": feed.get("coverUrl"),
        "published_at": _ms_to_str(feed.get("publishTime")),
        "views": _to_int(feed.get("vv")),
        "likes": _to_int(feed.get("likes")),
        "comments": _to_int(feed.get("comments")),
    }
    shares = _to_int(feed.get("shares"))
    if shares is not None:
        item["shares"] = shares
    if feed.get("isTop"):
        item["pinned"] = True
    return item


def fetch(
    *,
    account_file: str,
    account_name: str,
    with_media: bool = True,
    media_limit: int = 20,
    wait_timeout: float = 20.0,
) -> dict[str, Any]:
    """抓一个拼多多视频账号的数据（**阻塞**，内部开浏览器）。

    签名与 `myUtils/account_stats/base.fetch()` 转发的一致。异常语义：

    * 登录态失效 / 打不开页面 → `StatsUnavailable`（"拿不到"，界面提示而不是报错）；
    * 别的意外 → `AccountStatsError`。
    """
    from patchright.async_api import async_playwright

    account_path = _account_path(account_file)
    if not account_path.is_file():
        raise StatsUnavailable(f"账号 cookie 文件不存在：{account_file}")

    async def _run() -> dict[str, Any]:
        notes: list[str] = []
        captured: dict[str, Any] = {}
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=PDD_HEADLESS, channel="chromium"
            )
            try:
                context = await browser.new_context(storage_state=str(account_path))
                page = await context.new_page()

                # ① 发布页：账号信息 + 粉丝数
                captured.update(await _capture(page, _INFO_URL, {
                    "anchorInfo": _WANTED["anchorInfo"],
                    "anchorQuery": _WANTED["anchorQuery"],
                }, _PAGE_TIMEOUT))
                if "/login" in page.url:
                    raise StatsUnavailable(
                        "拼多多登录态已失效（被跳到登录页），请到「账号管理」重新扫码登录后再同步"
                    )

                # ② 数据总览：账号级总览 + 逐日序列
                captured.update(await _capture(page, _OVERVIEW_URL, {
                    "statistic": _WANTED["statistic"],
                }, _PAGE_TIMEOUT))

                # ③ 作品管理：作品列表
                if with_media:
                    captured.update(await _capture(page, _MANAGE_URL, {
                        "feeds": _WANTED["feeds"],
                    }, _PAGE_TIMEOUT))
            finally:
                await browser.close()
        return _build(captured, account_name, with_media, media_limit, notes)

    with browser_slot(wait_timeout):
        # 账号锁按 cookie 文件加：和发布任务互斥（同一个 cookie 不能同时开两个浏览器）
        with account_lock(account_path):
            try:
                return asyncio.run(asyncio.wait_for(_run(), timeout=_FETCH_TIMEOUT))
            except StatsUnavailable:
                raise
            except asyncio.TimeoutError as exc:
                raise AccountStatsError(
                    f"抓取拼多多账号数据超时（>{_FETCH_TIMEOUT:.0f} 秒）"
                ) from exc


def _build(
    captured: dict[str, Any],
    account_name: str,
    with_media: bool,
    media_limit: int,
    notes: list[str],
) -> dict[str, Any]:
    """把截到的几份响应拼成规范化结构。"""
    info = _result_of(captured.get("anchorInfo")) or {}
    query = _result_of(captured.get("anchorQuery")) or {}
    statistic = _result_of(captured.get("statistic")) or {}

    nickname = info.get("name") or query.get("anchorName")
    avatar = info.get("image") or query.get("anchorIcon")
    account_id = info.get("anchorUid") or query.get("anchorUid")
    fans_count = _to_int(info.get("fansNumDataTip")) or _to_int(info.get("fansNumTip"))
    if fans_count is None:
        notes.append("这次没拿到粉丝数（平台没返回 anchor/info，可能是页面没加载完）。")

    # ---- 账号级总览 + 逐日序列 ----
    overall: dict[str, int] = {}
    for block in statistic.get("overallStatistic") or []:
        if not isinstance(block, dict):
            continue
        field = _OVERALL_FIELDS.get(str(block.get("title") or "").strip())
        value = _to_int(block.get("value"))
        if field and value is not None:
            overall[field] = value

    periods: dict[str, Any] = {}
    series: dict[str, Any] = {}
    seven: dict[str, Any] = {}
    thirty: dict[str, Any] = {}
    for block in statistic.get("daysStatistic") or []:
        if not isinstance(block, dict):
            continue
        field = _DAILY_FIELDS.get(str(block.get("title") or "").strip())
        if not field:
            continue
        points = [
            {"date": point.get("date"), "value": _to_int(point.get("value")) or 0}
            for point in (block.get("dayValues") or [])
            if isinstance(point, dict) and point.get("date")
        ]
        if points:
            series[field] = points
        if block.get("sevenDaysAdd") is not None:
            seven[field] = _to_int(block.get("sevenDaysAdd"))
        if block.get("oneMonthAdd") is not None:
            thirty[field] = _to_int(block.get("oneMonthAdd"))
    if seven:
        periods["7d"] = seven
    if thirty:
        periods["30d"] = thirty
    if series:
        notes.append("逐日序列是平台给的最近 31 天。")

    # ---- 作品列表 ----
    media: dict[str, Any] = {"supported": False, "items": [], "total": None, "has_more": False}
    feeds_body = _result_of(captured.get("feeds")) or {}
    if with_media:
        feeds = [item for item in (feeds_body.get("feeds") or []) if isinstance(item, dict)]
        total = _to_int(feeds_body.get("total"))
        items = [_feed_item(feed) for feed in feeds[:media_limit]]
        media = {
            "supported": bool(items),
            "items": items,
            "total": total,
            "has_more": bool(total and total > len(items)),
            # 分享数平台经常给 null → 前端据此不显示这一列
            "share_metric_supported": any("shares" in item for item in items),
        }
        if not items:
            media["error"] = "平台上没有取到作品列表（可能这个账号还没发过作品）"
        elif media["has_more"]:
            notes.append(f"作品列表只取了最近 {len(items)} 条（平台共 {total} 条）。")
    else:
        # 说明放在 _build 里而不是 fetch 里：这条说明和"有没有取作品"是同一件事，
        # 放在一起才不会出现"两个地方各写一半"。
        notes.append("这次只取了账号与粉丝数据，没有取作品列表。")

    return build_stats(
        platform_no=5,
        account_name=account_name,
        nickname=nickname,
        avatar=avatar,
        account_id=str(account_id) if account_id else None,
        fans={"count": fans_count, "periods": {}},
        engagement={
            "periods": periods,
            "series": series,
            "lifetime": overall,
        },
        media=media,
        extra={
            "room_id": info.get("roomId") or query.get("roomId"),
            "certification": info.get("userCertificationStatus"),
        },
        notes=notes,
    )


__all__ = ["fetch"]
