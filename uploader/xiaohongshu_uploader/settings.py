# -*- coding: utf-8 -*-
"""小红书发布行为设置（at-ai-xhs 改造新增）。

背景
----
2026-03-10 小红书官方账号「薯管家」发布《关于打击 AI 托管运营账号的治理公告》，
明确禁止"利用技术手段模拟真人"，并对"与真人操作习惯不符"的行为特征分档处置
（偶发 → 警告/限制分发；完全由 AI 工具驱动或主页全量 AI 代发 → 封禁）。

所以发布链路上三件最扎眼的事被收敛掉了：

1. **无头发布** —— 最明显的行为特征，直接砍掉无头档。这一项**不在这里**，因为它是
   硬编码的强制行为（见 `main._launch_xhs(purpose="publish")` 与后端确认接口），
   刻意不留配置开关——留了迟早会被打开，然后回到被限流的老路上。
2. **发布按钮无界重试** —— 上游是 `while True` + 0.5s，被平台拦下会变成"每秒两点"。
3. **零延时整体输入 / 整点整分定时** —— 见 `utils/humanize.py`。

为什么要有这个模块
------------------
`conf.py` 是**本地文件（已在 .gitignore 里）**，每个部署点都保有一份自己的副本。
新加的设置项如果直接用 `from conf import XHS_XXX`，老副本上会立刻 ImportError，
把整条发布链路打断——一个纯配置项缺失不该有这个后果。

所以这里统一用**带默认值的读取**：conf 里有就用 conf 的，没有就用下面这份默认值。
默认值都取"安全的那一档"（有界重试、开人性化输入、开定时抖动），
所以配置缺失时行为是**更收敛**的，而不是退回旧的高风险默认。
"""

from __future__ import annotations

from typing import Any

import conf as _conf

# 与 conf.example.py 保持同步；改这里等于改所有没在 conf.py 里覆盖过的部署点。
DEFAULTS: dict[str, Any] = {
    # 发布按钮点击次数上限（上游是无限重试）
    "XHS_PUBLISH_MAX_ATTEMPTS": 3,
    # 标题/正文逐字输入的每字延时区间（毫秒）；None = 关闭人性化输入
    "XHS_HUMAN_TYPE_DELAY": (45, 140),
    # 单次人性化输入的总时长上限（秒），超过就等比压缩每字延时
    "XHS_HUMAN_TYPE_BUDGET": 150.0,
    # 定时发布时间的随机后移上限（分钟）；0 = 不抖动
    "XHS_SCHEDULE_JITTER_MINUTES": 20,
    # 浏览器上下文里跟"人"有关的几项；空值 = 不覆盖，沿用 Playwright 默认
    "XHS_LOCALE": "zh-CN",
    "XHS_TIMEZONE": "Asia/Shanghai",
    "XHS_VIEWPORT": {"width": 1440, "height": 900},
}


def _resolve(name: str) -> Any:
    """先读 conf，没有（或显式设成 None）就退回默认值。"""
    value = getattr(_conf, name, None)
    if value is None:
        return DEFAULTS[name]
    return value


# 模块级常量，方便 `from .settings import XHS_PUBLISH_MAX_ATTEMPTS` 这样直接引用
XHS_PUBLISH_MAX_ATTEMPTS: int = max(1, int(_resolve("XHS_PUBLISH_MAX_ATTEMPTS")))
XHS_HUMAN_TYPE_DELAY = _resolve("XHS_HUMAN_TYPE_DELAY")
XHS_HUMAN_TYPE_BUDGET: float = float(_resolve("XHS_HUMAN_TYPE_BUDGET"))
XHS_SCHEDULE_JITTER_MINUTES: int = int(_resolve("XHS_SCHEDULE_JITTER_MINUTES"))
XHS_LOCALE = _resolve("XHS_LOCALE")
XHS_TIMEZONE = _resolve("XHS_TIMEZONE")
XHS_VIEWPORT = _resolve("XHS_VIEWPORT")

__all__ = [
    "DEFAULTS",
    "XHS_PUBLISH_MAX_ATTEMPTS",
    "XHS_HUMAN_TYPE_DELAY",
    "XHS_HUMAN_TYPE_BUDGET",
    "XHS_SCHEDULE_JITTER_MINUTES",
    "XHS_LOCALE",
    "XHS_TIMEZONE",
    "XHS_VIEWPORT",
]
