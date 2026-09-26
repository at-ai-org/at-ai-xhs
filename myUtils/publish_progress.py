# -*- coding: utf-8 -*-
"""发布进度上报桥（at-ai-xhs 改造新增）。

发布中心要显示"每个平台 / 每个账号 / 每个任务此刻在干什么"，但四个平台的上传器
都是上游代码（`uploader/*/main.py`），为了前端展示去大改它们不划算。这里做一层薄桥：

1. **显式上报**：执行器在关键节点调用 `report(stage, text)`；
2. **日志旁听**：同时挂一个 loguru sink，把上传器自己打的日志
   （"小人正在努力上传视频"…）按关键字映射成粗粒度阶段，并保留原始日志尾巴，
   供前端滚动显示。

为什么敢从日志里推断阶段：上传器的日志本来就是"这一步在干什么"的旁白，而且措辞
在四个平台之间是统一的（见 `_STAGE_PATTERNS`）。推断结果**只用来给阶段名和进度条**，
前端显示的"当前动作"永远是日志原文——推断不准也不会误导用户。

线程模型
--------
进度按**线程**绑定（`threading.local`）：发布中心每个任务在自己的线程里跑，
任务线程里所有日志都归到这个任务上。浏览器操作是全局串行的，所以同一时刻
只会有一次绑定生效，不会串台。
"""

from __future__ import annotations

import threading
from typing import Callable

from utils.log import logger

# --------------------------------------------------------------------------- #
# 阶段定义
# --------------------------------------------------------------------------- #

STAGE_PREPARING = "preparing"
STAGE_LAUNCHING = "launching"
STAGE_UPLOADING = "uploading"
STAGE_FILLING = "filling"
STAGE_SUBMITTING = "submitting"
STAGE_VERIFYING = "verifying"

# 顺序即"推进方向"：进度条只前进不后退，避免日志措辞变化让进度来回跳。
STAGES: tuple[str, ...] = (
    STAGE_PREPARING,
    STAGE_LAUNCHING,
    STAGE_UPLOADING,
    STAGE_FILLING,
    STAGE_SUBMITTING,
    STAGE_VERIFYING,
)

STAGE_ORDER: dict[str, int] = {name: index for index, name in enumerate(STAGES)}

STAGE_TEXT: dict[str, str] = {
    STAGE_PREPARING: "准备任务",
    STAGE_LAUNCHING: "启动浏览器",
    STAGE_UPLOADING: "上传视频",
    STAGE_FILLING: "填写文案",
    STAGE_SUBMITTING: "提交发布",
    STAGE_VERIFYING: "校验结果",
}

# 日志关键字 → 阶段。**先匹配先生效**，所以把更具体的短语放前面：
# 例如"已经进入 version_1 发布页面"必须落到 launching，而不是被"发布"误判成 submitting。
_STAGE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        STAGE_SUBMITTING,
        (
            "冲刺发布",
            "正在发布",
            "提交发布",
            "点击发布",
            "发布按钮",
            "准备发布",
            "开始发布",
            "定时发布",
            "设置定时",
        ),
    ),
    (
        STAGE_VERIFYING,
        ("校验发布", "确认是否发布成功", "检查发布结果", "发布后校验"),
    ),
    (
        STAGE_UPLOADING,
        (
            "上传视频",
            "搬运视频",
            "努力上传",
            "还在等视频",
            "正在上传",
            "上传完成",
            "传完",
            "上传失败",
            "重新上传",
            "上传图文",
            "上传图片",
            "等图片上传",
        ),
    ),
    (
        STAGE_FILLING,
        (
            "填标题",
            "填描述",
            "填写",
            "话题",
            "设置封面",
            "商品链接",
            "自主声明",
            "添加到合集",
            "选合集",
        ),
    ),
    (
        STAGE_LAUNCHING,
        (
            "检查通过",
            "赶往",
            "正在打开",
            "进入",
            "启动浏览器",
            "登录",
            "扫码",
            "验证",
        ),
    ),
    (
        STAGE_PREPARING,
        ("检查 cookie", "上传前检查", "开始搬运"),
    ),
)


def stage_for_text(text: str) -> str | None:
    """把一条日志映射成阶段，映射不出来返回 None（阶段保持不变）。"""
    if not text:
        return None
    for stage, keywords in _STAGE_PATTERNS:
        for keyword in keywords:
            if keyword in text:
                return stage
    return None


# --------------------------------------------------------------------------- #
# 按线程绑定 + 日志旁听
# --------------------------------------------------------------------------- #

_local = threading.local()

# handler 收到的事件：
#   {"type": "stage", "stage": "uploading", "text": "…"|None}
#   {"type": "log",   "stage": "uploading"|None, "text": "…", "level": "INFO"}
Handler = Callable[[dict], None]


def bind(handler: Handler, business: str | None = None) -> None:
    """把当前线程的上报绑到 `handler`。

    `business` 是 loguru 的业务名（`utils/log.py` 里 `logger.bind(business_name=…)`），
    只旁听该业务名的日志，避免把别的模块的输出算到本任务头上。
    """
    _local.handler = handler
    _local.business = business


def unbind() -> None:
    _local.handler = None
    _local.business = None


def report(stage: str, text: str | None = None) -> None:
    """显式上报一个阶段（执行器在关键节点调用）。"""
    handler = getattr(_local, "handler", None)
    if handler is None:
        return
    try:
        handler({"type": "stage", "stage": stage, "text": text})
    except Exception:
        # 上报失败绝不能影响发布本身
        pass


def _sink(message) -> None:
    """loguru sink：把上传器的日志转发给当前线程绑定的任务。

    必须是"拿不到 handler 就立刻返回"——这个 sink 对**所有**日志都会触发，
    包括没有任务在跑的时候。
    """
    handler = getattr(_local, "handler", None)
    if handler is None:
        return
    try:
        record = message.record
        business = getattr(_local, "business", None)
        if business and record["extra"].get("business_name") != business:
            return
        text = record["message"]
        if not text:
            return
        handler(
            {
                "type": "log",
                "stage": stage_for_text(text),
                "text": text,
                "level": record["level"].name,
            }
        )
    except Exception:
        pass


# 挂 DEBUG 级：上传器最细的进度旁白（"小人还在等视频上传完成"）是 debug 级，
# 少了它们就看不到"正在慢慢传"的过程。
logger.add(_sink, level="DEBUG", format="{message}")

__all__ = [
    "STAGES",
    "STAGE_ORDER",
    "STAGE_TEXT",
    "STAGE_PREPARING",
    "STAGE_LAUNCHING",
    "STAGE_UPLOADING",
    "STAGE_FILLING",
    "STAGE_SUBMITTING",
    "STAGE_VERIFYING",
    "bind",
    "unbind",
    "report",
    "stage_for_text",
]
