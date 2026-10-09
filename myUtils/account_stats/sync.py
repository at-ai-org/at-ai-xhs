# -*- coding: utf-8 -*-
"""账号数据同步：把平台侧的粉丝 / 互动 / 作品数据抓回来覆盖本地库（at-ai-xhs 改造新增）。

为什么单独一层、不直接调 `fetch()`
----------------------------------
同一件事（"抓一个账号的最新数据并落库"）现在有**三个入口**要共用：

1. **会话续期定时任务**（`myUtils/session_renew.py`，默认 0/7/14/20 点）——
   续期本来就要给每个账号开一次浏览器，数据同步顺手在同一轮里做掉；
2. **详情页的「同步」按钮**——用户想立刻看最新的；
3. **账号刚扫码加进来时**——刚登录完就把数据拉一次，账号管理页第一眼就有数字。

如果三处各写一遍"抓 + 落库 + 处理失败"，最迟在下一次改字段时就会漏掉一处。
所以这里收成一个函数 `sync_account()`，三个入口都调它。

"覆盖"的确切含义
----------------
* 抓成功 → **整行覆盖** `account_stats`（`cache.save` 就是 UPSERT，主键是账号 id）；
* 抓失败 → **不写库**。这一点是刻意的：失败时把旧数据擦掉换成一份空壳，
  等于"用一次网络抖动换掉了本来好好的历史数据"。旧数据留着，详情页照常能看，
  只是弹窗上会标一句"这次没更新成功"。
* 不支持 / 未登录（`StatsUnavailable`）→ 同样不写库，但结论是"这个平台/账号暂时拿不到"，
  和真正的出错分开（界面上一个是提示、一个是红色报错）。

返回什么
--------
统一返回 `{"status": ..., "message": ..., ...}`，`status` 的取值和
`session_renew.STATUS_*` 一套：`ok` / `invalid` / `busy` / `error`。
这样续期那轮可以直接把它当成自己的一行结果拼进日志和界面，不用再翻译一遍。
"""

from __future__ import annotations

import time
from typing import Any

from myUtils.account_stats import AccountStatsError, StatsUnavailable
from myUtils.account_stats import cache

#: 三个入口共用的结论码（和 `session_renew.STATUS_*` 一致）。
STATUS_OK = "ok"
STATUS_INVALID = "invalid"
STATUS_BUSY = "busy"
STATUS_ERROR = "error"

#: 「同步」等待浏览器锁的上限（秒）。
#:
#: 比详情页那条路（20 秒）短一些：同步是**批量**动作（定时任务要跑完整个账号表），
#: 在一个账号上干等半分钟会把它后面所有账号都推后。等不到就跳过——这一轮没同步上
#: 不要紧，下一个时间点还会再来一次，而"卡住不动"才是真的难看。
SYNC_LOCK_WAIT = 15.0


def sync_account(
    account: dict,
    *,
    with_media: bool = True,
    media_limit: int = 20,
    wait_timeout: float = SYNC_LOCK_WAIT,
) -> dict[str, Any]:
    """抓一个账号的最新数据并**覆盖**本地库。**同步阻塞**，失败不抛异常。

    `account` 是 `session_renew.list_accounts()` 那种形状的字典
    （`id` / `type` / `file_path` / `name` / `platform`），
    这样续期那轮可以把它自己的账号行直接传进来。

    `with_media=False` 可以跳过作品列表——只要粉丝和互动数字时能快不少
    （少几次分页请求）。
    """
    from myUtils.account_stats import fetch as fetch_stats
    from myUtils.browser_lock import BrowserLockTimeout, browser_slot

    account_id = int(account.get("id") or 0)
    base: dict[str, Any] = {
        "id": account_id,
        "name": account.get("name") or "",
        "platform": account.get("platform") or "",
        "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    platform_no = int(account.get("type") or 0)
    file_name = account.get("file_path") or ""

    if not account_id or not file_name:
        return {**base, "status": STATUS_ERROR, "message": "账号信息不完整（缺少 id 或 cookie 文件名）"}

    # 不需要浏览器的平台（抖音是纯 HTTP）就别去抢并发名额：抢了等于白占一个浏览器位置。
    needs_browser = platform_no != 3

    def _fetch() -> dict[str, Any]:
        return fetch_stats(
            platform_no,
            file_name,
            account.get("name") or "",
            with_media=with_media,
            media_limit=media_limit,
            wait_timeout=wait_timeout,
        )

    started = time.monotonic()
    try:
        if needs_browser:
            # 只占「整机并发名额」；**账号锁由适配器自己上**（它才知道该独占什么：
            # 小红书 / 快手是 cookie 文件，视频号是持久化 profile）。这里再按 cookie
            # 文件上一把，会和适配器那把套起来，而且视频号锁的根本不是同一个东西。
            with browser_slot(wait_timeout):
                stats = _fetch()
        else:
            stats = _fetch()
    except BrowserLockTimeout as exc:
        return {
            **base,
            "status": STATUS_BUSY,
            "message": f"浏览器名额已满，本轮跳过同步：{exc}",
        }
    except StatsUnavailable as exc:
        # "平台拿不到"不是错误：不写库（旧的留着），但结论要说得出来。
        return {**base, "status": STATUS_INVALID, "message": str(exc)}
    except AccountStatsError as exc:
        return {**base, "status": STATUS_ERROR, "message": f"同步失败：{exc}"}
    except Exception as exc:  # 适配器里没预料到的异常不该让整轮任务中断
        return {**base, "status": STATUS_ERROR, "message": f"同步出错：{exc}"}

    cache.save(account_id, platform_no, account.get("name") or "", stats)

    # 顺手把头像落到本地（avatars/<账号id>.<ext>），前端列表用它。
    # 这一步刻意放在"已经确认抓成功"之后、而且**失败不影响同步结论**：
    # 没有头像只是列表上少张图，粉丝/作品那些主数据不该被它拖累。
    avatar_note = ""
    try:
        from myUtils.account_stats.avatar import store_avatar

        if store_avatar(account_id, (stats.get("account") or {}).get("avatar")):
            avatar_note = "、头像已更新"
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 头像落地失败（不影响同步）：{exc}")

    media = stats.get("media") or {}
    media_count = len(media.get("items") or []) if media.get("supported") else None
    fans = (stats.get("fans") or {}).get("count")
    parts = []
    if fans is not None:
        parts.append(f"粉丝 {fans}")
    if media_count:
        parts.append(f"作品 {media_count}")
    if avatar_note:
        parts.append(avatar_note.lstrip("、"))
    detail = f"（{'、'.join(parts)}）" if parts else ""
    return {
        **base,
        "status": STATUS_OK,
        "message": f"数据已同步{detail}",
        "elapsed": round(time.monotonic() - started, 1),
        "fans_count": fans,
    }


__all__ = [
    "STATUS_BUSY",
    "STATUS_ERROR",
    "STATUS_INVALID",
    "STATUS_OK",
    "SYNC_LOCK_WAIT",
    "sync_account",
]
