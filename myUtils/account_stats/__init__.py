# -*- coding: utf-8 -*-
"""账号数据（粉丝 / 互动）抓取（at-ai-xhs 改造新增）。

「账号管理」页那张表原来只有名称 / 平台 / 状态三列，全是**本地就知道**的东西。
本包负责把**平台侧**的数据拉回来：粉丝数、涨粉、以及自己作品的点赞 / 收藏 / 评论 /
分享 / 播放。入口是 `fetch()`，平台适配器在 `xhs.py` / `douyin.py` / `kuaishou.py` /
`tencent.py`，各自的实现细节与坑都写在对应的模块里。
"""

from .base import (
    AccountStatsError,
    StatsUnavailable,
    build_stats,
    canonicalize,
    fetch,
    platform_name,
    platform_supported,
)

__all__ = [
    "AccountStatsError",
    "StatsUnavailable",
    "build_stats",
    "canonicalize",
    "fetch",
    "platform_name",
    "platform_supported",
]
