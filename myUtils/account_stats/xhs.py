# -*- coding: utf-8 -*-
"""小红书账号数据适配器（at-ai-xhs 改造新增）。

抓什么
------
「账号管理」页点「详情」时，这个适配器会把下面这些**平台侧**数据拉回来：

| 数据 | 来源接口 | 对应界面 |
| --- | --- | --- |
| 粉丝数 / 关注数 / 获赞与收藏 | `/api/galaxy/creator/home/personal_info` | 账号概览卡片 |
| 涨粉 / 掉粉 / 净涨粉（近 7 天、近 30 天） | `/api/galaxy/v2/creator/datacenter/account/base` | 「粉丝数据」页 |
| 逐日涨粉趋势（7 天 / 30 天） | 同上 `net_rise_fans_count_list` | 「粉丝数据」页的迷你图 |
| 播放 / 点赞 / 收藏 / 评论 / 分享 / 完播率 | 同上 | 「互动数据」页 |
| 每条笔记的播放 / 点赞 / 收藏 / 评论 / 分享 | `/api/galaxy/v2/creator/note/user/posted` | 「作品数据」页 |

怎么绕开签名（这是本模块存在的**唯一**理由）
------------------------------------------
小红书的 `/api/galaxy/**` 全部要求 `X-s` / `X-t` 签名，直接 `requests` 打过去是
**406**（实测：`/api/galaxy/v2/creator/datacenter/account/base`，带齐 cookie 也一样）。
上游 `uploader/xhs_uploader/main.py` 的解法是 `sign_local()`：**每次调用都开一个新浏览器**
去 `www.xiaohongshu.com` 注入 `a1` cookie，再 `evaluate(window._webmsxyw)` 求签名——
一次调用开一个 chromium，只为拿两个 HTTP 头。

这里换成另一条路，理由如下：

1. **创作服务平台自己的页面里就有签名函数**。登录态进 `creator.xiaohongshu.com` 之后，
   `window._webmsxyw` 是**现成的**（实测 `typeof window._webmsxyw === "function"`）。
   我们本来就要开一个浏览器去读数据，签名顺手就能拿到，不用为它再开第二个。
2. **在页面里 fetch 能白拿一整套登录态**：`credentials: 'include'` 沿用页面自己的 cookie、
   `a1`、设备指纹，不用手工把 storage_state 里的 cookie 一条条搬进 requests，
   也就不会出现"搬漏一条 `web_session` 就 406"这种查半天的毛病。
3. 实测三个接口（`personal_info`、`account/base`、`note/user/posted`）在页面内
   `fetch` 全部 200 并返回真实数据——**签名是自己现算的，不是捡页面已经发出去的那个**
   （这点很关键：捡现成签名的方案要求先手动点进"数据看板"，多一步交互且随时会失效）。

代价是"读一次数据 ≈ 开一次浏览器"（几秒），所以 Web 层会缓存结果，见
`sau_backend_stats.py` 里的说明。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from conf import BASE_DIR

from .base import AccountStatsError, StatsUnavailable, build_stats

#: 登录/加载页面本身 + 等 `_webmsxyw` 就绪的上限（秒）。
#: 实测冷启动 3~6 秒；给到 60 是留足弱网和平台变慢的余量。
_PAGE_READY_TIMEOUT = 60.0

#: 整个抓取流程（开浏览器 → 读数据 → 关浏览器）的上限（秒）。
_FETCH_TIMEOUT = 180.0

#: 逐条抓「每条笔记数据」是 N 次额外请求，太慢也更容易触发风控。
#: 作品列表默认只取首页（平台一页 10 条），需要更多时由调用方传 `media_limit`。
_POSTED_PAGE_SIZE = 10

#: 取签名 + 发起请求的页面内脚本。
#:
#: 逐条细节：
#: * `_webmsxyw(uri, data)` 的第一个参数是**带 query 的路径**（不是完整 URL），
#:   GET 时第二个参数传 `undefined`、POST 时传**要提交的对象**——传错了拿到的签名无效。
#: * 签名重试：页面刚加载完时 `_webmsxyw` 偶尔会拿不到 key（内部还没初始化好），
#:   这里退避重试 8 次。上游 `sign_local()` 是"整只浏览器重开"重试 10 次，
#:   代价太大；同一个页面里重试就够了。
#: * 超时用 `AbortController` 自己控制，不用 `fetch` 的默认"永不超时"。
_FETCH_JS = r"""
async ([path, method, body]) => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  if (typeof window._webmsxyw !== 'function') {
    return { ok: false, error: 'SIGN_FN_MISSING' };
  }
  let signed = null;
  let lastErr = null;
  for (let i = 0; i < 8; i++) {
    try {
      const data = method === 'GET' ? undefined : (body || {});
      const enc = await window._webmsxyw(path, data);
      if (enc && enc['X-s'] && enc['X-t'] !== undefined) { signed = enc; break; }
      lastErr = 'EMPTY_SIGN';
    } catch (e) {
      lastErr = String(e);
    }
    await sleep(300 + i * 250);
  }
  if (!signed) {
    return { ok: false, error: 'SIGN_FAILED: ' + lastErr };
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 30000);
  try {
    const opt = {
      method,
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        'X-s': signed['X-s'],
        'X-t': String(signed['X-t']),
      },
      signal: controller.signal,
    };
    if (method !== 'GET') opt.body = JSON.stringify(body || {});
    const resp = await fetch(path, opt);
    const text = await resp.text();
    return { ok: true, status: resp.status, body: text };
  } catch (e) {
    return { ok: false, error: 'FETCH_FAILED: ' + String(e) };
  } finally {
    clearTimeout(timer);
  }
}
"""


def _account_file_path(account_file: str) -> Path:
    """`user_info.filePath`（文件名）→ cookiesFile/ 下的绝对路径。"""
    return Path(BASE_DIR) / "cookiesFile" / str(account_file or "")


def _parse_json(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise AccountStatsError(f"小红书返回的不是 JSON（{raw[:120]}）") from exc
    if not isinstance(data, dict):
        raise AccountStatsError("小红书返回的结构不是对象")
    return data


def _ms_to_date(ms: Any) -> str | None:
    """毫秒时间戳 → `YYYY-MM-DD`（本地时间）。

    直接用 `fromtimestamp` 走本地时区：这些日期在平台上本来就是按**用户本地时区**
    聚合的（`begin_time` / `end_time` 也同源），用 UTC 换算会让"今天"整体偏一天。
    """
    try:
        from datetime import datetime

        return datetime.fromtimestamp(float(ms) / 1000).strftime("%Y-%m-%d")
    except Exception:
        return None


def _pct(value: Any) -> Any:
    """`*_rate` 字段是**相对上期的百分比变化**（平台返回的整数，如 1759 = +1759%）。

    不过滤、不四舍五入，原样透传——前端负责加上 `%` 和正负号。
    """
    return value


# --------------------------------------------------------------------------- #
# 页面操作
# --------------------------------------------------------------------------- #


async def _call(page, path: str, method: str = "GET", body: Any = None) -> dict[str, Any]:
    """在页面里签一次名并请求 `path`，返回解析后的 JSON。

    签名函数放在页面里算（见模块 docstring 第 1 点的说明），这里只负责：
    调用、把 `SIGN_FAILED` 之类的技术性失败翻译成**能看懂的中文**。
    """
    result = await page.evaluate(_FETCH_JS, [path, method, body])

    if not result.get("ok"):
        error = str(result.get("error") or "")
        if error == "SIGN_FN_MISSING":
            raise AccountStatsError(
                "小红书的签名脚本没有加载出来（页面可能没有正常打开）。"
                "常见原因是登录态已失效——先在账号管理里点一下「重新扫码」。"
            )
        if error.startswith("SIGN_FAILED"):
            raise AccountStatsError(f"小红书请求签名失败，请稍后重试（{error}）")
        raise AccountStatsError(f"小红书接口请求失败：{error}")

    status = result.get("status")
    payload = _parse_json(result.get("body") or "")

    if status != 200:
        # 406 是最常见的一种：签名头没被接受（平台改了签名算法）。
        # 单独说清楚，因为它的处理方式和"cookie 失效"完全不同。
        if status == 406:
            raise StatsUnavailable(
                "小红书拒绝了这次请求（406，签名未通过）。"
                "通常是平台调整了签名算法，需要更新工具版本。"
            )
        raise AccountStatsError(
            f"小红书接口返回 {status}：{str(payload)[:160]}"
        )

    # 业务层的失败：`success: false` 时 data 为空、msg 里带原因（例如登录态失效）。
    if payload.get("success") is False:
        msg = payload.get("msg") or payload.get("message") or "未知原因"
        if payload.get("code") in (-1, 401, 10001):
            raise StatsUnavailable(f"小红书返回：{msg}（登录态可能已失效，可尝试重新扫码）")
        raise AccountStatsError(f"小红书接口报错：{msg}")

    return payload


async def _prepare_page(context):
    """打开创作服务平台首页，并等到页面的签名函数就绪。"""
    page = await context.new_page()
    try:
        await page.goto(
            "https://creator.xiaohongshu.com/new/home",
            wait_until="domcontentloaded",
            timeout=45000,
        )
    except Exception as exc:
        raise AccountStatsError(f"打不开小红书创作服务平台：{exc}") from exc

    # 等 `_webmsxyw` 出现。它由页面自己的 JS 注入，`domcontentloaded` 时通常还没好。
    try:
        await page.wait_for_function("typeof window._webmsxyw === 'function'", timeout=30000)
    except Exception:
        # 没等到也别立刻判死：可能是登录跳转到了登录页，也可能只是慢。
        # 交给后面 `_call` 报一条更有信息量的错（SIGN_FN_MISSING）。
        pass
    return page


# --------------------------------------------------------------------------- #
# 响应 → 规范化结构
# --------------------------------------------------------------------------- #


#: 后端字段 → 平台上的「逐日序列」字段名。
#:
#: 小红书的 account/base 里，每个指标都**成对**出现：一个汇总值（`view_count`）
#: 和一个逐日数组（`view_list`）。名字对不上是常态（`view_count` ↔ `view_list`、
#: `like_count` ↔ `like_list`、`collect_count` ↔ `collect_list`），**不能靠加 `_list`
#: 后缀去猜**——`home_view_count` 的序列叫 `home_view_list`（少个 count），
#: `rise_fans_count` 叫 `rise_fans_list`。所以这里显式列出来，写错了也只是少一张图，
#: 不会串到别的指标上。
_SERIES_FIELDS: dict[str, str] = {
    "view_count": "view_list",
    "impl_count": "impl_count_list",
    "home_view_count": "home_view_list",
    "like_count": "like_list",
    "collect_count": "collect_list",
    "comment_count": "comment_list",
    "share_count": "share_list",
    "danmaku_count": "danmaku_list",
    "rise_fans_count": "rise_fans_list",
    "loss_fans_count": "loss_fans_count_list",
    "net_rise_fans_count": "net_rise_fans_count_list",
    "publish_note_num": "publish_note_num_list",
    "publish_video_note_num": "publish_video_note_num_list",
    "avg_view_time": "avg_view_time_list",
    "video_full_view_rate": "video_full_view_rate_list",
    "cover_click_rate": "cover_click_rate_list",
}


def _series_from_count_list(items: Any, *, key: str = "count") -> list[dict[str, Any]]:
    """把 `[{date: ms, count: n}]` 转成 `[{date: "YYYY-MM-DD", count: n}]`，按日期升序。

    平台返回的列表是**倒序**的（最近的在最前），画趋势图要正序，统一在这里翻过来，
    免得前端每处都自己 reverse。
    """
    out: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        date = _ms_to_date(item.get("date"))
        if not date:
            continue
        out.append({"date": date, "count": item.get(key)})
    out.sort(key=lambda row: row["date"])
    return out


def _period_block(block: dict[str, Any]) -> dict[str, Any]:
    """把平台的一个周期块（`seven` / `thirty`）压成规范化的一段。

    只挑界面要用的字段。平台上还有一大堆 `set_xxx: true` 的能力开关位
    （"这个指标平台愿不愿意给"），对用户没有意义，全部丢掉；
    真正要判断"有没有"就直接看字段在不在。
    """
    if not isinstance(block, dict):
        return {}

    fields = {
        "view_count": "播放量",
        "impl_count": "曝光量",
        "home_view_count": "首页推荐曝光",
        "like_count": "点赞",
        "collect_count": "收藏",
        "comment_count": "评论",
        "share_count": "分享",
        "danmaku_count": "弹幕",
        "rise_fans_count": "涨粉",
        "loss_fans_count": "掉粉",
        "net_rise_fans_count": "净涨粉",
        "publish_note_num": "发布笔记数",
        "publish_video_note_num": "发布视频数",
        "avg_view_time": "人均观看时长(秒)",
        "video_full_view_rate": "完播率(%)",
        "cover_click_rate": "封面点击率(%)",
    }
    out: dict[str, Any] = {}
    series: dict[str, list[dict[str, Any]]] = {}
    for key, label in fields.items():
        if key in block and block[key] is not None:
            out[key] = block[key]
            # 顺带把它的逐日明细也取出来（界面上的迷你趋势图靠它）。
            # 取不到就不放 —— 前端会显示「—」，而不是画一条假线。
            source = _SERIES_FIELDS.get(key)
            if source:
                trend = _series_from_count_list(block.get(source))
                if len(trend) > 1:
                    series[key] = trend
    if series:
        out["series"] = series
    # 环比（相对上一周期的百分比变化）。没有就不放，避免前端画出"0%"这种假趋势。
    rates = {
        key: _pct(block[key])
        for key in block
        if key.endswith("_rate") and not key.endswith("_rate_display")
        and key[:-5] in out and block[key] is not None
    }
    if rates:
        out["rates"] = rates
    # 平台自己给的结论（带 <font> 标签，前端渲染前会剥掉）。
    if block.get("summary"):
        out["summary"] = str(block["summary"])
    return out


def _fans_section(personal: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """粉丝相关数据：当前值 + 各周期的涨跌 + 逐日趋势。"""
    fans: dict[str, Any] = {
        "count": personal.get("fans_count"),
        "following_count": personal.get("follow_count"),
        # 「获赞与收藏」是小红书主页展示的累计值，不是 7/30 天的区间值。
        "likes_total": personal.get("faved_count"),
        "periods": {},
    }
    if personal.get("grow_info"):
        # 平台的"成长等级"进度（当前粉丝 / 下一档门槛），界面上做成进度条。
        fans["growth"] = personal["grow_info"]

    seven = base.get("seven") or {}
    thirty = base.get("thirty") or {}

    for key, block in (("7d", seven), ("30d", thirty)):
        if not isinstance(block, dict):
            continue
        period: dict[str, Any] = {}
        for src, dst in (
            ("rise_fans_count", "gained"),
            ("loss_fans_count", "lost"),
            ("net_rise_fans_count", "net"),
        ):
            if block.get(src) is not None:
                period[dst] = block[src]
        if block.get("rise_fans_count_rate") is not None:
            period["gain_rate"] = _pct(block["rise_fans_count_rate"])
        period["rise_series"] = _series_from_count_list(block.get("rise_fans_list"))
        period["loss_series"] = _series_from_count_list(block.get("loss_fans_list"))
        period["net_series"] = _series_from_count_list(block.get("net_rise_fans_count_list"))
        # 涨粉总览链接数（"首页转化涨粉"这类），有就带上
        if block.get("home_conversion_rise_fans_rate") is not None:
            period["home_conversion_rate"] = block["home_conversion_rise_fans_rate"]
        fans["periods"][key] = period

    return fans


def _engagement_section(base: dict[str, Any], personal: dict[str, Any]) -> dict[str, Any]:
    """互动数据：各周期的播放/点赞/收藏/评论/分享。"""
    engagement: dict[str, Any] = {"periods": {}}
    for key, src in (("7d", "seven"), ("30d", "thirty")):
        block = base.get(src) or {}
        if isinstance(block, dict) and block:
            engagement["periods"][key] = _period_block(block)
    # 累计值也附上：账号主页上展示的那三个数字，用户拿来和平台对得上。
    engagement["lifetime"] = {
        "likes_and_collects": personal.get("faved_count"),
        "fans": personal.get("fans_count"),
        "following": personal.get("follow_count"),
    }
    return engagement


def _media_item(note: dict[str, Any]) -> dict[str, Any]:
    """一条笔记 → 规范化的一行。

    字段名对齐平台（`likes` 而不是 `like_count`），因为这是"作品列表"，
    前端要展示的列和平台自己的作品管理页一致，命名一致便于对照排查。
    """
    images = note.get("images_list") or []
    cover = None
    if images and isinstance(images[0], dict):
        cover = images[0].get("url")
    video_info = note.get("video_info") or {}
    return {
        "id": note.get("id"),
        "title": note.get("display_title") or note.get("title") or "（无标题）",
        "type": note.get("type"),
        "cover": cover,
        "published_at": note.get("time"),
        "duration": video_info.get("duration") if isinstance(video_info, dict) else None,
        "views": note.get("view_count"),
        "likes": note.get("likes"),
        "collects": note.get("collected_count"),
        "comments": note.get("comments_count"),
        "shares": note.get("shared_count"),
        # 笔记链接：小红书用私有 scheme，网页端点不开，但复制到 App 里可用。
        "url": f"https://www.xiaohongshu.com/explore/{note.get('id')}" if note.get("id") else None,
    }


async def _collect(page, *, with_media: bool, media_limit: int) -> dict[str, Any]:
    """把需要的接口读一遍。任何一个失败都会往上抛，由 `fetch()` 决定怎么收场。"""
    personal_payload = await _call(page, "/api/galaxy/creator/home/personal_info")
    personal = personal_payload.get("data") or {}

    base_payload = await _call(page, "/api/galaxy/v2/creator/datacenter/account/base")
    base = base_payload.get("data") or {}

    media: dict[str, Any] = {"supported": True, "items": [], "total": None, "has_more": False}
    notes_out: list[str] = []

    if with_media:
        # 作品列表是**独立的一页数据**，它挂了不该让整个详情页变成空白，
        # 所以这里单独 try：失败就在 notes 里说明，粉丝/互动数据照常展示。
        try:
            page_index = 0
            while len(media["items"]) < media_limit:
                payload = await _call(
                    page,
                    f"/api/galaxy/v2/creator/note/user/posted?tab=0&page={page_index}",
                )
                data = payload.get("data") or {}
                batch = data.get("notes") or []
                if not batch:
                    break
                media["items"].extend(_media_item(n) for n in batch if isinstance(n, dict))
                if len(batch) < _POSTED_PAGE_SIZE:
                    media["has_more"] = False
                    break
                page_index += 1
                media["has_more"] = True
            if len(media["items"]) > media_limit:
                media["items"] = media["items"][:media_limit]
            media["total"] = len(media["items"])
        except Exception as exc:
            media["supported"] = False
            media["error"] = str(exc)
            notes_out.append(f"作品列表没有取到：{exc}")
    else:
        media["supported"] = False
        notes_out.append("这次只取了账号与粉丝数据，没有取作品列表。")

    return {
        "personal": personal,
        "base": base,
        "media": media,
        "notes": notes_out,
    }


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
            f"账号「{account_name}」的 cookie 文件不存在（{cookie_path.name}），"
            "需要先扫码登录。"
        )

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=LOCAL_CHROME_HEADLESS)
        except Exception as exc:
            raise AccountStatsError(f"启动浏览器失败：{exc}") from exc
        try:
            # 用账号自己的 storage_state 建上下文。**不用** XHS 发布那条
            # "持久化 profile" 链路：这里只是只读查询，一次性 context 不会污染
            # 发布用的设备指纹；而持久化 profile 会和发布任务抢同一把锁。
            context = await browser.new_context(
                storage_state=str(cookie_path),
                locale=XHS_LOCALE or "zh-CN",
                timezone_id=XHS_TIMEZONE or "Asia/Shanghai",
                viewport=XHS_VIEWPORT or {"width": 1440, "height": 900},
            )
            try:
                # 顺手加载反检测脚本，和发布链路保持一致的页面环境
                try:
                    stealth = Path(BASE_DIR) / "utils" / "stealth.min.js"
                    if stealth.is_file():
                        await context.add_init_script(path=str(stealth))
                except Exception:
                    pass

                page = await _prepare_page(context)
                collected = await _collect(
                    page, with_media=with_media, media_limit=media_limit
                )
            finally:
                await context.close()
        finally:
            await browser.close()

    personal = collected["personal"]
    base = collected["base"]

    if not personal:
        raise StatsUnavailable(
            f"小红书没有返回账号信息（账号「{account_name}」）。"
            "登录态可能已失效，可以试试「重新扫码」或「上传Cookie」。"
        )

    return build_stats(
        platform_no=1,
        account_name=account_name,
        nickname=personal.get("name"),
        avatar=personal.get("avatar"),
        account_id=personal.get("red_num"),
        fans=_fans_section(personal, base),
        engagement=_engagement_section(base, personal),
        media=collected["media"],
        extra={
            "desc": personal.get("personal_desc"),
            # 创作者平台的权限清单（有没有开数据中心、创作学院等）
            "diagnosis_status": personal.get("diagnosis_status"),
        },
        notes=collected["notes"],
    )


def fetch(
    account_file: str,
    account_name: str,
    *,
    with_media: bool = True,
    media_limit: int = 20,
    wait_timeout: float = 20.0,
) -> dict[str, Any]:
    """抓一个小红书账号的数据（**阻塞**，必须在工作线程里调用）。

    `browser_session` 用的是 **cookiesFile 下那个 cookie 文件**当锁的 key：
    小红书的发布链路（工作台 / 发布中心）锁的是 `cookies/xiaohongshu_<account>.json`，
    两条路锁的文件不一样，所以**读数据不会被发布任务挡住**，也不会互相顶掉登录态。
    这是刻意的——查数据是只读的，没必要排在一次几分钟的发布后面。
    """
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
