# -*- coding: utf-8 -*-
"""视频号（channels.weixin.qq.com）账号数据适配器（at-ai-xhs 改造新增）。

⚠️ **这个适配器的"能不能跑通"还没有被真实数据验证过**，原因见下面第 2 点。
写出来是因为**接口契约是确凿的**，而"取数走错 profile、永远报未登录"那个 bug
（见下面第 1 点）一直挡在前面，2026-10-09 才修掉。

抓什么
------
| 数据 | 来源接口 | 对应界面 |
| --- | --- | --- |
| 粉丝数 / 头像 / 昵称 | `/cgi-bin/mmfinderassistant-bin/statistic/fans_trend`（`total`）+ `auth_wx` | 账号概览卡片 |
| 净增 / 新增 / 取消关注（逐日） | `statistic/fans_trend` | 粉丝数据页 |
| 播放 / 喜欢 / 推荐 / 评论 / 分享 / 完播率 | `statistic/post_total_data` | 互动数据页 |
| 每条视频的播放、点赞、收藏、评论、分享、涨粉 | `statistic/post_list` | 作品数据页 |

字段名不是猜的，是从平台自己的前端 bundle 里读出来的
----------------------------------------------------
视频号的数据中心是个 wujie 微前端（`/micro/statistic/`），前端 bundle 是公开的。
下面这些 key 就是从 bundle 里的指标映射表逐条挖出来的（`[中文名, key]` 原样）：

    播放→readCount  推荐→likeCount  喜欢→favCount  评论→commentCount
    分享→forwardAggregationCount  关注→followCount  完播率→fullPlayRate
    净增关注→netAdd  新增关注→add  取消关注→reduce  关注者总数→total

⚠️ 注意**视频号的"推荐"和"喜欢"跟别家不一样**：`likeCount` 在界面上叫「推荐」，
`favCount` 叫「喜欢」。本项目统一按"用户直觉"映射——`likeCount` → 点赞（前端列名"点赞"）、
`favCount` → 收藏。这样四个平台的作品表能并排看，代价是和视频号后台的字面文案对不上。
想核对数字时请以平台后台为准（`notes` 里也写了一句）。

⚠️ 上面这套长名**只适用于作品列表那一行**。账号级的汇总接口 `post_total_data` 用的是短名
（`browse` / `like` / `comment` / `forward` / `fav`），照 bundle 的长名去认会一个都对不上，
互动那一块就整段是空的（2026-10-09 实测，两套名字见 `_TOTAL_FIELDS` 的注释）。

两件必须说清楚的事
------------------
1. **登录态在持久化 profile 里，不在 cookie 文件里**。视频号是服务端会话，
   证书轮换频繁，cookie 文件经常是过期的。所以这里走
   `uploader/tencent_uploader/session.py` 的 `tencent_session()`（和登录 / 校验 / 发布
   同一条链路），并且 **`seed=False`**：不把 cookie 文件里的值灌进 profile。
   理由很直接——文件里那条 `sessionid` 可能已经死了，灌进去会把 profile 里**还能用**的
   会话顶掉。profile 才是这个平台的权威登录态。

   ⚠️ 但**`seed=False` 必须和 `profile_dir=` 一起用**。在 session 层里，`seed=False`
   的含义不是"别灌 cookie"，而是"用一个全新的临时 profile"（扫码登录那条路就是这么用的，
   它连 `account_file` 都不传）。只传 `account_file` + `seed=False` 会落到
   `new_staging_dir()` 那个 `_login_<随机>` 空白目录上——**里面一条登录态都没有**，
   打开数据中心会被整页跳到登录页，于是"账号明明是好的，却永远报未登录"。

   这不是假设，是 2026-10-09 的真实事故：账号 10:00:21 刚扫码、10:00:34 / 10:01:23 /
   10:03:33 三次 `cookie_auth` 全部「cookie 有效」（它走的是 `uin_<微信号>` 那个 profile），
   而同一分钟的取数仍然报"未登录状态"，用户为此反复重扫；日志里每次取数都留下一个
   ~18MB 的 `_login_*` 目录，一天攒了 15 个。回归测试见
   `tests/test_account_stats.py::TencentSessionProfileTest`。
2. **已经打到过真实响应了（2026-10-09 修掉上面那个 bug 之后），但只验证到一个账号**。
   当时探明的几件事都写进了代码和测试里，别再当"待确认"看：
   * 关注者总数取 `fans_trend` 的 `total`（逐日累计，最后一段就是当前值）——实测 13，对得上；
   * `post_total_data` 的数组是"每天一个数"的平行数组，**要 N 天给 N+1 段**（见 `_daily_series`）；
   * 昵称 / 头像在 `auth_wx` 的 **`userAttr`** 里，不在顶层；而且那个头像地址
     （`wx.qlogo.cn/mmhead/ver_1/…`）**必须补上尺寸后缀**才能下载，否则 CDN 返回 400
     （见 `_avatar_url`）；
   * 账号级汇总**不给**涨粉数和完播率（作品列表里才有个 `fullPlayRate`），
     所以 `rise_fans_count` / `video_full_view_rate` 在互动那一段通常是缺的——
     缺就是缺，不要显示成 0。

   所以现在它是"真跑通过一次"，不是"每个号都验证过"。换账号或平台改版后仍要按现象重新核。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from conf import BASE_DIR

from .base import AccountStatsError, StatsUnavailable, build_stats

#: 接口前缀。微前端自己的 baseURL 就是这个。
_URL_PREFIX = "/cgi-bin/mmfinderassistant-bin"

#: 数据中心的页面地址。**必须在页面里 fetch**，不能拿去给 requests 用：
#: `_aid` / `_rid` 这类参数平台不校验（实测手工拼的值也能进业务逻辑），但 cookie
#: 是 httpOnly 的、而且会话绑定浏览器上下文，`requests` 拿不到那条会话。
_PAGE_URL = "https://channels.weixin.qq.com/platform/statistic"

#: 页面加载上限（秒）。视频号是**微前端**（主应用里再套 `/micro/statistic/`），
#: 比普通页面慢，给的余量比小红书那两个大。
_PAGE_READY_TIMEOUT = 90.0

#: 整体抓取上限（秒）。视频号要开持久化 profile（比一次性 context 慢）。
_FETCH_TIMEOUT = 240.0

#: 一次取多少天的区间数据。7 / 30 两个窗口各取一次。
_WINDOWS: dict[str, int] = {"7d": 7, "30d": 30}

#: 请求超时（毫秒，页面内计时）。
_FETCH_MS = 30000


def _account_file_path(account_file: str) -> Path:
    return Path(BASE_DIR) / "cookiesFile" / str(account_file or "")


# --------------------------------------------------------------------------- #
# 页面内请求
# --------------------------------------------------------------------------- #

#: 在页面里 POST 一个接口。
#:
#: `_rid` / `_aid` / `_pageUrl` 参数照前端 `addRidMiddleware` / `addAidMiddleware` 的
#: 做法拼（实测这些**不校验**，写什么都能进业务逻辑，所以这里给一个格式正确的值就够）；
#: `X-WECHAT-UIN` 也照前端带上（同样不强制）。
_POST_JS = r"""
async ([path, body]) => {
  const rid = Math.floor(Date.now() / 1000).toString(16) + '-' +
              Array.from({length: 8}, () => '0123456789abcdef'[Math.floor(Math.random()*16)]).join('');
  const aid = (() => { try { return localStorage.getItem('__ml::aid') || ''; } catch (e) { return ''; } })();
  const qs = new URLSearchParams({_aid: aid, _rid: rid, _pageUrl: location.href}).toString();
  const url = path + '?' + qs;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), %(timeout)d);
  try {
    const r = await fetch(url, {
      method: 'POST',
      credentials: 'include',
      headers: {'Content-Type': 'application/json', 'X-WECHAT-UIN': '0000000000'},
      body: JSON.stringify(body || {}),
      signal: controller.signal,
    });
    return {ok: true, status: r.status, body: await r.text()};
  } catch (e) {
    return {ok: false, error: String(e)};
  } finally {
    clearTimeout(timer);
  }
}
""" % {"timeout": _FETCH_MS}

#: 判断页面还在不在登录页。
#:
#: 视频号登出时会**整页跳到 `login.html`**，微前端根本不会挂起来（所有路由都重定向过去）。
#: 所以"当前 URL 是不是 login.html"是最可靠的失效信号，比解析接口的 errCode 还早一步。
_LOGGED_OUT_JS = "() => location.href.includes('/login')"


async def _call(page, path: str, body: Any) -> dict[str, Any]:
    """在页面里请求一个接口，返回 `data`。"""
    result = await page.evaluate(_POST_JS, [path, body])

    if not result.get("ok"):
        raise AccountStatsError(f"视频号接口请求失败：{result.get('error')}")

    try:
        payload = json.loads(result.get("body") or "")
    except ValueError as exc:
        raise AccountStatsError(f"视频号返回的不是 JSON（{str(result.get('body'))[:120]}）") from exc
    if not isinstance(payload, dict):
        raise AccountStatsError("视频号返回的结构不是对象")

    err_code = payload.get("errCode", payload.get("errcode"))
    if err_code in (0, None):
        return payload.get("data") or {}

    message = payload.get("errMsg") or payload.get("errmsg") or "未知原因"
    # 300333 = 会话失效（实测登出状态下每个接口都返回它）。它不是"接口坏了"，
    # 而是"要重新登录"——所以归到 StatsUnavailable（界面上一句人话），不是 error。
    if err_code == 300333:
        raise StatsUnavailable(
            f"视频号的登录态已失效（{message}）。"
            "视频号的会话是服务端轮换的，过期比其它平台快——"
            "到「账号管理」对这一行点「重新扫码」就能恢复。"
        )
    raise AccountStatsError(f"视频号接口返回异常（{err_code} {message}）")


# --------------------------------------------------------------------------- #
# 响应 → 规范化结构
# --------------------------------------------------------------------------- #


def _daily_series(
    raw: Any, *, start_ts: int, days: int, key: str | None = None
) -> list[dict[str, Any]]:
    """把视频号的"逐日数组"转成 `[{date, count}]`。

    视频号的时间序列是**平行数组**：`data.netAdd = [1,0,2,...]`，第 i 个数对应
    `startTs + i 天`。所以日期得自己按起点推。

    **每天要 N+1 段，不是 N 段**（2026-10-09 打到真实接口才确认）：`interval=3` 是"按天"，
    要 7 天返回 **8** 个数、要 30 天返回 **31** 个——首尾都算在内（最后一段是今天，通常还没过完）。
    实测 7 天那份就是 30 天那份的最后 8 个，所以"从 startTs 数第 i 天"的算法是对的，
    但**不能按 `days` 截断**：那样会正好把"今天"这一段丢掉，而它往往是刚发完视频那天。
    （`interval=1` 是 10 分钟粒度，一天 144 段；本项目只按天看，所以固定发 `interval=3`。）
    30 天那份实测 `[..., 36, 30, 69, 20, 17, 13, 17, 1]`，末尾 8 个与 7 天那份逐位相同。

    两种形状都认：
    * `[1, 0, 2]` —— 每天一个数（**实测就是这个形状**）；
    * `[{"count": 1}, ...]` 或 `[{"value": 1}, ...]` —— 每天一个对象（保留兼容）。
    认不出来就返回空列表（界面上少画一张图），比画错强。
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    base = datetime.fromtimestamp(start_ts)
    for index, item in enumerate(raw):
        if index > days:
            # 兜底：正常只会多出 1 段（今天）。多得多说明平台改了口径，
            # 与其画出几十个没有日期的点，不如只收到该收的那一段。
            break
        value: Any
        if isinstance(item, dict):
            value = item.get(key or "count")
            if value is None:
                value = item.get("value")
            # 有些口径把日期放在对象里，优先用它（更可靠）
            if item.get("date"):
                out.append({"date": str(item["date"]), "count": _num(value)})
                continue
        else:
            value = item
        date = (base + timedelta(days=index)).strftime("%Y-%m-%d")
        out.append({"date": date, "count": _num(value)})
    return [row for row in out if row["count"] is not None]


def _num(value: Any) -> Any:
    """把平台给的值转成数字。

    **小数必须原样保留**：完播率是 `0.5` / `0.0432` 这种小数，先 `int()` 截断会得到 0，
    界面上就显示成"完播率 0%"——账号明明有完播（这个 bug 被
    `tests/test_account_stats.py` 抓出来过）。

    注意：**不能靠"int() 抛异常就退回 float()"** 来判断，因为 `int(0.5)` 不抛异常、
    直接返回 0（截断）。所以先看原始类型，只有字符串才需要试探着转 int
    （视频号有些计数是 `"100"` 这种字符串）。

    整数最后长什么样交给 `canonicalize()` 统一收口：`25.0` 会变成 `25`，
    而 `0.0432` 不动。
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return None


def _last_number(raw: Any) -> Any:
    """取一个逐日数组里**最后一个非空值**（用来表示"当前总数"）。"""
    if not isinstance(raw, list):
        return None
    for item in reversed(raw):
        value = _num(item.get("value") if isinstance(item, dict) else item)
        if value is not None:
            return value
    return None


#: `post_total_data` 的 key → 规范化字段名。
#:
#: ⚠️ **汇总接口和作品列表用的不是同一套字段名**（2026-10-09 打到真实接口才发现）。
#: 作品列表那一行确实是 `readCount` / `likeCount` / `favCount` / `commentCount` /
#: `forwardAggregationCount`（见 `_post_item`，已实测），但**账号级的汇总**给的是短名：
#:
#:     {"browse": [...], "like": [...], "comment": [...], "forward": [...], "fav": [...]}
#:
#: 原来只按长名去认，于是互动那几个数字整段是空的——界面看着像"这个号没有任何互动"。
#: 两套名字都留着：短名是实测的，长名留着兼容其它口径（认不到就不写 key，前端不画）。
#:
#: 「推荐 / 喜欢」的改名理由见模块 docstring：短名 `like` 对应界面上的「推荐」、
#: `fav` 对应「喜欢」，本项目统一显示成点赞 / 收藏。
_TOTAL_FIELDS: dict[str, str] = {
    # 实测（账号级汇总）
    "browse": "view_count",
    "like": "like_count",
    "fav": "collect_count",
    "comment": "comment_count",
    "forward": "share_count",
    # 兼容：作品行 / 其它口径的长名
    "readCount": "view_count",
    "likeCount": "like_count",
    "favCount": "collect_count",
    "commentCount": "comment_count",
    "forwardAggregationCount": "share_count",
    "followCount": "rise_fans_count",
    "avgPlayTimeSec": "avg_view_time",
}

#: **比率类**字段：不能求和（三个 50% 加起来是 150%，毫无意义），只取区间最后一个值。
#:
#: 单独列一份而不是靠在 `_TOTAL_FIELDS` 里判断，是因为"该不该求和"取决于指标语义，
#: 跟数据长什么样无关——用"值看起来像不像比例"去猜，某个月的完播率恰好是 1.0 就会猜错。
#:
#: ⚠️ 实测**账号级汇总不返回完播率**（`post_total_data` 里只有 browse/like/comment/forward/fav），
#: 所以这一段通常是缺的；作品列表里那一行才有 `fullPlayRate`。缺就是缺，别显示 0。
_RATE_FIELDS: dict[str, str] = {
    "fullPlayRate": "video_full_view_rate",
}


def _period_from_total_data(data: dict[str, Any], start_ts: int, days: int) -> dict[str, Any]:
    """`post_total_data` → 互动数据的一段（数值 + 逐日趋势）。"""
    period: dict[str, Any] = {}
    series: dict[str, list[dict[str, Any]]] = {}

    for source_key, field in _TOTAL_FIELDS.items():
        if source_key not in data:
            continue
        raw = data[source_key]
        trend = _daily_series(raw, start_ts=start_ts, days=days, key="count")
        if trend:
            # 总量用"逐日求和"——它和下面的趋势图是同一份数据，不会出现
            # "总数 100 但图上加起来 98"这种对不上的情况。
            period[field] = sum(row["count"] for row in trend)
            series[field] = trend
        else:
            value = _num(raw)
            if value is not None:
                period[field] = value

    for source_key, field in _RATE_FIELDS.items():
        if source_key not in data:
            continue
        trend = _daily_series(data[source_key], start_ts=start_ts, days=days, key="count")
        if trend:
            series[field] = trend
            value = trend[-1]["count"]
        else:
            value = _num(data[source_key])
        if value is not None:
            # 平台给的是 0~1 的小数（和快手同一个口径），统一换算成百分数
            period[field] = round(value * 100, 2) if value <= 1 else value

    if series:
        period["series"] = series
    return period


def _post_item(row: dict[str, Any]) -> dict[str, Any]:
    """一条视频 → 规范化的一行。"""
    desc = row.get("desc") or {}
    object_id = row.get("objectId") or row.get("exportId")

    created = _num(row.get("createTime"))
    published_at = None
    if created:
        # 视频号的 createTime 是**秒**（和抖音一样）
        published_at = datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M")

    full_play = row.get("fullPlayRate")
    try:
        full_play = round(float(full_play) * 100, 2) if full_play and float(full_play) <= 1 else full_play
    except (TypeError, ValueError):
        full_play = None

    return {
        "id": str(object_id) if object_id else None,
        "title": (desc.get("description") if isinstance(desc, dict) else "") or "（无标题）",
        "type": "video",
        "cover": row.get("coverUrl") or row.get("cover"),
        "published_at": published_at,
        "duration": _num(row.get("playDuration")) or _num(row.get("duration")),
        "views": _num(row.get("readCount")),
        "likes": _num(row.get("likeCount")),
        "collects": _num(row.get("favCount")),
        "comments": _num(row.get("commentCount")),
        "shares": _num(row.get("forwardAggregationCount")),
        "rise_fans": _num(row.get("followCount")),
        "full_view_rate": full_play,
        # 视频号没有公开的网页播放页，只能复制 id 到后台查
        "url": None,
    }


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #


async def _collect(
    page, *, with_media: bool, media_limit: int
) -> dict[str, Any]:
    """按窗口把接口读一遍。"""
    now = int(datetime.now().timestamp())
    periods: dict[str, Any] = {}
    fans_periods: dict[str, Any] = {}
    fans_total: Any = None

    for period_key, days in _WINDOWS.items():
        start_ts = now - days * 86400

        # 粉丝趋势
        try:
            trend = await _call(
                page,
                f"{_URL_PREFIX}/statistic/fans_trend",
                # `startTs` / `endTs` 平台收的是**字符串**（前端就是这么发的）
                {"interval": 3, "startTs": str(start_ts), "endTs": str(now)},
            )
        except StatsUnavailable:
            raise
        except AccountStatsError:
            trend = {}

        net_series = _daily_series(trend.get("netAdd"), start_ts=start_ts, days=days)
        add_series = _daily_series(trend.get("add"), start_ts=start_ts, days=days)
        reduce_series = _daily_series(trend.get("reduce"), start_ts=start_ts, days=days)
        if net_series or add_series or reduce_series:
            fans_periods[period_key] = {
                "net": (
                    sum(row["count"] for row in net_series) if net_series else None
                ),
                "gained": sum(row["count"] for row in add_series) if add_series else None,
                "lost": sum(row["count"] for row in reduce_series) if reduce_series else None,
                "net_series": net_series,
                "rise_series": add_series,
                "loss_series": reduce_series,
            }
        # 关注者总数：`total` 是逐日的累计值，最后一个就是当前值。
        # **待确认**：真实响应里这个 key 也可能是账号对象上的 `fansCount`；
        # 下面读不到时会退回 `auth_wx`。
        if fans_total is None:
            fans_total = _last_number(trend.get("total"))

        # 作品总量数据
        try:
            total_data = await _call(
                page,
                f"{_URL_PREFIX}/statistic/post_total_data",
                {"interval": 3, "startTs": str(start_ts), "endTs": str(now)},
            )
        except StatsUnavailable:
            raise
        except AccountStatsError:
            total_data = {}
        if isinstance(total_data, dict) and total_data:
            block = _period_from_total_data(total_data, start_ts, days)
            if block:
                periods[period_key] = block

    media: dict[str, Any] = {"supported": True, "items": [], "total": None, "has_more": False}
    notes: list[str] = []
    if with_media:
        try:
            data = await _call(
                page,
                f"{_URL_PREFIX}/statistic/post_list",
                {
                    "pageSize": max(1, min(media_limit, 50)),
                    "currentPage": 1,
                    "sort": 1,
                    "order": 1,
                    "startTime": 0,
                    "endTime": now,
                },
            )
            rows = data.get("list") or []
            media["items"] = [_post_item(row) for row in rows if isinstance(row, dict)]
            total = _num(data.get("totalCount"))
            media["total"] = total if total is not None else len(media["items"])
            media["has_more"] = bool(total and total > len(media["items"]))
            notes.append("视频号后台把「推荐」叫点赞、「喜欢」叫收藏，这里的列名按通俗叫法显示。")
        except (AccountStatsError, StatsUnavailable) as exc:
            media["supported"] = False
            media["error"] = str(exc)
            notes.append(f"作品列表没有取到：{exc}")
    else:
        media["supported"] = False
        notes.append("这次只取了账号与粉丝数据，没有取作品列表。")

    # 头像 / 昵称（顺带补关注者总数）
    profile: dict[str, Any] = {}
    try:
        auth = await _call(page, f"{_URL_PREFIX}/auth/auth_wx", {})
        if isinstance(auth, dict):
            profile = auth
    except (AccountStatsError, StatsUnavailable):
        # `auth_wx` 拿不到不算失败：昵称/头像没有就显示账号管理里的名字。
        profile = {}

    return {
        "periods": periods,
        "fans_periods": fans_periods,
        "fans_total": fans_total,
        "media": media,
        "profile": profile,
        "notes": notes,
    }


async def _fetch_async(
    account_file: str,
    account_name: str,
    *,
    with_media: bool,
    media_limit: int,
) -> dict[str, Any]:
    from playwright.async_api import async_playwright

    from uploader.tencent_uploader.session import profile_dir_for, tencent_session

    account_path = _account_file_path(account_file)
    if not account_path.exists():
        # 提前判掉，而不是让浏览器去跑一遍：账号文件没了（手工删过、换机器）
        # 时给一句人话，也省掉一次十几秒的空转。
        raise StatsUnavailable(
            f"账号「{account_name}」的视频号登录文件不在了（{account_path.name}），"
            "到「账号管理」对这一行点「重新扫码」即可。"
        )

    async with async_playwright() as playwright:
        # `seed=False`：**不要把 cookie 文件里的 cookie 灌进 profile**。
        # 视频号是服务端会话，文件里那条 sessionid 很可能已经失效，灌进去会把 profile
        # 里还能用的会话顶掉——这正是"查一次数据反而把账号搞掉线"的经典事故。
        #
        # 但 `seed=False` **必须**配 `profile_dir=`：在 session 层里它的含义是"用全新的临时
        # profile"，而不是"别灌 cookie"。只传 seed=False 会落到 `_login_<随机>` 那个空 profile
        # 上（见模块 docstring 第 1 点，2026-10-09 的事故就是这么来的）。
        # 显式给账号自己的 profile 目录，才是本意：读 profile 里现成的会话、不拿文件去顶它。
        # `fallback_when_busy=True`：只读查询不该排队等发布任务；profile 被占用时
        # 降级成一次性 context 看一眼（拿不到就报登录态问题，而不是卡住）。
        # `persist=False`：只读操作不回写账号文件，避免把失效票据写回去。
        async with tencent_session(
            playwright,
            account_path,
            profile_dir=profile_dir_for(account_path),
            seed=False,
            persist=False,
            fallback_when_busy=True,
        ) as session:
            context = session.context
            page = await context.new_page()
            try:
                await page.goto(
                    _PAGE_URL, wait_until="domcontentloaded", timeout=int(_PAGE_READY_TIMEOUT * 1000)
                )
            except Exception as exc:
                # 同 kuaishou.py：打不开页面是网络瞬断，归「这次没拿到」而不是硬错误
                raise StatsUnavailable(f"打不开视频号助手：{exc}") from exc

            await page.wait_for_timeout(3000)

            # 登出状态：整页会被重定向到 login.html，微前端不会挂起来。
            # 提前判一次，比等到接口层报 300333 更早、提示也更准。
            try:
                if await page.evaluate(_LOGGED_OUT_JS):
                    raise StatsUnavailable(
                        f"账号「{account_name}」在视频号这边是未登录状态"
                        "（页面被跳到了登录页）。到「账号管理」对这一行点「重新扫码」即可。"
                    )
            except StatsUnavailable:
                raise
            except Exception:
                # `evaluate` 自己失败（页面正在跳转等）不该当结论用，交给下面的接口报错。
                pass

            try:
                return await _collect(page, with_media=with_media, media_limit=media_limit)
            finally:
                try:
                    await page.close()
                except Exception:
                    pass


def _avatar_url(raw: Any) -> str | None:
    """把 `auth_wx` 给的头像地址收拾成**能下载**的那个形式。

    实测（2026-10-09）：`https://wx.qlogo.cn/mmhead/ver_1/<hash>` 这种"微信头像"地址
    **必须带尺寸后缀**，直接请求会得到 `HTTP 400` 且响应体是空的；加上 `/0`（原图）
    或 `/132`（132px 缩略）才是 200 + `image/jpeg`。

    所以这里补 `/0`：本地只存一张，原图也就 30KB 左右，比缩略图省不了多少，
    放大显示时还不会糊。

    只在"看起来就是这类地址、且末尾没有尺寸"时才动它——别的 CDN
    （小红书的、快手的）本来就没有这个规矩，乱加后缀反而会 404。
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text.startswith("http"):
        return text or None
    head, _, query = text.partition("?")
    if "wx.qlogo.cn/mmhead/" in head and not head.rstrip("/").rsplit("/", 1)[-1].isdigit():
        return f"{head}/0" + (f"?{query}" if query else "")
    return text


def fetch(
    account_file: str,
    account_name: str,
    *,
    with_media: bool = True,
    media_limit: int = 20,
    wait_timeout: float = 20.0,  # 签名和其它平台对齐，见下方说明
) -> dict[str, Any]:
    """抓一个视频号账号的数据（**阻塞**，必须在工作线程里调用）。

    走的是视频号自己的持久化 profile（`tencent_session`），profile 锁由它内部的
    `browser_lock.account_lock(profile_dir)` 管着——所以发布任务在跑时这里会降级成
    只读探测，而不是排队等几分钟。
    """
    try:
        collected = asyncio.run(
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
    except asyncio.TimeoutError as exc:
        raise AccountStatsError(
            f"读取视频号数据超过 {int(_FETCH_TIMEOUT)} 秒还没完成，已放弃。"
            "视频号要开持久化浏览器 profile，比其它平台慢；可以稍后重试。"
        ) from exc

    profile = collected.get("profile") or {}
    # `auth_wx` 的真实响应（2026-10-09 实测）：
    #   {"displayWordingList": [], "userAttr": {"nickname": "…",
    #     "encryptedUsername": "AOA****888",
    #     "encryptedHeadImage": "https://wx.qlogo.cn/mmhead/ver_1/…"}, "finderLoginStatus": 1}
    # 昵称 / 头像都在 **userAttr 里面**，不在顶层——原来按顶层取，所以界面上昵称和头像
    # 一直是空的（账号管理页那个名字是本地账号名，不是平台昵称）。
    # `encryptedUsername` 是打过码的（`AOA****888`），当不了账号 id，所以 id 仍然留空。
    user_attr = profile.get("userAttr") if isinstance(profile.get("userAttr"), dict) else {}
    fans_total = collected.get("fans_total")
    if fans_total is None:
        # `auth_wx` 没有粉丝数（实测），只作为兜底：万一别的口径给了 fansCount
        fans_total = _num(user_attr.get("fansCount") or profile.get("fansCount") or profile.get("fans_count"))

    raw_avatar = (
        user_attr.get("encryptedHeadImage") or profile.get("headImgUrl") or profile.get("headImg")
    )
    if isinstance(raw_avatar, dict):
        raw_avatar = raw_avatar.get("url")
    avatar = _avatar_url(raw_avatar)
    nickname = user_attr.get("nickname") or profile.get("nickname") or profile.get("nickName")

    fans: dict[str, Any] = {
        "count": fans_total,
        "periods": collected.get("fans_periods") or {},
    }

    media = collected.get("media") or {"supported": False, "items": [], "total": None, "has_more": False}
    notes = collected.get("notes") or []
    if not fans_total and not (collected.get("periods") or {}):
        notes.append("视频号这次没有返回数据；如果它是未登录状态，请先重新扫码。")

    return build_stats(
        platform_no=2,
        account_name=account_name,
        nickname=nickname,
        avatar=avatar,
        account_id=str(profile.get("finderId") or profile.get("uin") or "") or None,
        fans=fans,
        engagement={"periods": collected.get("periods") or {}},
        media=media,
        notes=notes,
    )


__all__ = ["fetch"]
