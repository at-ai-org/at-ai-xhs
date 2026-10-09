# -*- coding: utf-8 -*-
"""把平台头像**落到本地**（at-ai-xhs 改造新增）。

为什么不让前端直接引平台的头像地址
----------------------------------
账号详情的同步结果里本来就带远程头像 URL（`stats.account.avatar`），但直接把它塞进
`<el-avatar>` 有三个问题：

1. **热链**：各平台 CDN 对空 Referer / 跨站引用不一定放行（小红书、视频号都有这类先例），
   表现是头像随机裂图；
2. **会过期**：部分平台的头像 URL 带签名或时效，过一阵就 403；
3. **离线/内网**：本地工具的使用场景里，浏览器不一定总能连到平台 CDN。

所以同步时顺手把图片**下载到 `avatars/<账号id>.<扩展名>`**，前端改成请求本地路由
（`GET /account-stats/avatar/<id>`），拿不到就回退成原来的"文字头像"。

不重复下载
----------
同步会在三个入口发生（详情页同步 / 定时续期顺带同步 / 新账号登录后），
每次都重下头像纯属浪费。这里在文件旁边放一个 `<账号id>.url` 记录来源 URL：
URL 没变就直接复用本地那张图。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from conf import BASE_DIR

#: 头像落地目录（已加进 .gitignore，属于运行时产物）。
AVATAR_DIR = Path(BASE_DIR) / "avatars"

#: 允许的扩展名（按 Content-Type / URL 猜出来之后会收敛到这几个）。
_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

#: 单张头像的体积上限（超过就当异常，避免把一个奇怪的响应写进磁盘）。
_MAX_BYTES = 2 * 1024 * 1024

#: 下载超时（秒）。头像很小，几秒足够；失败也不该拖慢同步。
_TIMEOUT = 15

#: 有些 CDN 对空 User-Agent 直接拒，给一个常见的。
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
}


def _guess_ext(url: str, content_type: str | None) -> str:
    """先从 Content-Type 认，其次从 URL 路径里认，最后兜底 .jpg。"""
    if content_type:
        ext = _EXT_BY_CONTENT_TYPE.get(content_type.split(";")[0].strip().lower())
        if ext:
            return ext
    path = url.split("?")[0].lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


def avatar_file(account_id: int) -> Path | None:
    """这个账号本地有没有头像，返回文件路径（没有返回 None）。"""
    directory = AVATAR_DIR
    if not directory.is_dir():
        return None
    for candidate in sorted(directory.glob(f"{int(account_id)}.*")):
        if candidate.suffix == ".url":
            continue
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def store_avatar(account_id: int, url: str | None) -> str | None:
    """把头像下载到本地，返回落地文件名（拿不到就返回 None，**不抛异常**）。

    同步链路上的一步，失败只该记日志：没有头像不影响粉丝/作品这些主数据。
    """
    account_id = int(account_id or 0)
    if not account_id or not url or not str(url).startswith("http"):
        return None

    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    url = str(url).strip()
    url_marker = AVATAR_DIR / f"{account_id}.url"

    # 来源 URL 没变、本地图还在 → 直接复用，不重复下载
    existing = avatar_file(account_id)
    if existing is not None:
        try:
            if url_marker.is_file() and url_marker.read_text(encoding="utf-8").strip() == url:
                return existing.name
        except Exception:  # noqa: BLE001 —— 标记文件读不了就当缓存未命中
            pass

    try:
        import requests

        response = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        if response.status_code != 200 or not response.content:
            print(f"⚠️ 头像下载失败（HTTP {response.status_code}）：{url[:80]}")
            return None
        if len(response.content) > _MAX_BYTES:
            print(f"⚠️ 头像体积异常（{len(response.content) / 1024:.0f}KB），跳过：{url[:80]}")
            return None

        ext = _guess_ext(url, response.headers.get("Content-Type"))
        target = AVATAR_DIR / f"{account_id}{ext}"
        # 先写临时文件再原子替换：并发同步（定时任务 + 手动点同步）时不会读到半张图
        tmp = AVATAR_DIR / f".{account_id}.{uuid.uuid4().hex}.part"
        tmp.write_bytes(response.content)
        os.replace(tmp, target)
        # 换了扩展名的话，把旧文件清掉（例如原来 .jpg，平台改成了 .png）
        for stale in AVATAR_DIR.glob(f"{account_id}.*"):
            if stale.suffix != ".url" and stale != target:
                try:
                    stale.unlink()
                except Exception:  # noqa: BLE001
                    pass
        try:
            url_marker.write_text(url, encoding="utf-8")
        except Exception:  # noqa: BLE001 —— 标记写不了只是下次会重下，不影响本次结果
            pass
        return target.name
    except Exception as exc:  # noqa: BLE001 —— 头像失败绝不该影响同步结论
        print(f"⚠️ 头像下载异常（不影响同步）：{exc}")
        return None


__all__ = ["AVATAR_DIR", "avatar_file", "store_avatar"]
