"""人性化输入节奏与时间抖动。

用途：把发布链路上最扎眼的三个机器特征降下来。

1. **零延时整体输入** —— `keyboard.type(text)` 不带 `delay` 时几百字几毫秒打完；
   `locator.fill()` 更彻底，直接设 value，压根不产生逐键 keydown/keyup。
2. **无界重试** —— 被平台校验拦下后以固定间隔（0.5s）反复点击提交。
   （这一项不在这里实现，见 `conf.XHS_PUBLISH_MAX_ATTEMPTS` 与发布循环。）
3. **整点整分的定时发布** —— 真人预约不会卡在 20:00:00。

定位说明：这个模块只做「降低机械特征」这一件事——让输入节奏和时间点带上合理的
人类噪声。它**不是**反检测工具，不做指纹伪造、不做环境伪装。账号安全依赖的是
自动化占比真的下降（人工确认闸门 + 真人编辑文案），而不是让机器装得更像人。
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from typing import Any

# 句读/换行处多停一拍：真人打字在标点后会有停顿
_PAUSE_AFTER = set("，。！？；：、,.!?;:…—\n")

# 单字延时兜底区间（毫秒），当调用方没给或给了非法值时用
_FALLBACK_DELAY = (45, 140)


def normalize_delay(delay: Any) -> tuple[int, int] | None:
    """把配置里的延时区间规整成 (min_ms, max_ms)。

    None / 非法值 / 反向区间 一律返回 None（= 关闭人性化输入），
    避免配置写错时抛异常把发布流程带崩。
    """
    if delay is None:
        return None
    try:
        lo, hi = delay
        lo, hi = int(lo), int(hi)
    except (TypeError, ValueError):
        return None
    if lo < 0 or hi < 0 or lo > hi:
        return None
    return lo, hi


def _scale_for_budget(length: int, lo: int, hi: int, budget_s: float) -> tuple[int, int]:
    """按总时长预算等比压缩延时。

    长正文逐字打会很久（1000 字 × 90ms ≈ 90s）。超过预算就整体压缩，
    保证发布不会因为"打得太慢"卡住或被超时打断。
    """
    if length <= 0 or budget_s <= 0:
        return lo, hi
    avg_ms = (lo + hi) / 2.0
    projected_s = length * avg_ms / 1000.0
    if projected_s <= budget_s:
        return lo, hi
    factor = budget_s / projected_s
    return max(1, int(lo * factor)), max(1, int(hi * factor))


async def human_type(
    page,
    text: str,
    *,
    delay: Any = _FALLBACK_DELAY,
    budget_s: float | None = None,
    long_pause_chance: float = 0.06,
) -> None:
    """逐字输入，带随机延时，标点后偶发长停顿。

    行为与 `page.keyboard.type(text)` 保持一致（`\\n` 走 Enter 键），
    区别只在节奏——所以可以直接替换掉原来的一次性整体输入，不会改变页面语义。

    `delay` 传 None 时直接走原生 `keyboard.type`（等于关闭人性化输入）。
    """
    if not text:
        return

    span = normalize_delay(delay)
    if span is None:
        await page.keyboard.type(text)
        return

    lo, hi = span
    if budget_s:
        lo, hi = _scale_for_budget(len(text), lo, hi, budget_s)

    for ch in text:
        if ch == "\n":
            await page.keyboard.press("Enter")
        else:
            await page.keyboard.type(ch)
        await asyncio.sleep(random.uniform(lo, hi) / 1000.0)
        # 标点后停久一点；另外偶发一次"想一下"的长停顿
        if ch in _PAUSE_AFTER:
            await asyncio.sleep(random.uniform(80, 320) / 1000.0)
        elif random.random() < long_pause_chance:
            await asyncio.sleep(random.uniform(200, 700) / 1000.0)


def jitter_schedule(
    when: datetime,
    max_minutes: int | float | None,
    *,
    floor_to_minute: bool = True,
) -> tuple[datetime, int]:
    """给定时发布时间加随机抖动，返回 (抖动后时间, 实际推移的分钟数)。

    `max_minutes` 为 None / 0 / 负数时原样返回（不抖动），方便按配置关掉。
    抖动方向只往后推，避免推到过去导致平台拒绝（平台要求至少 1 小时后）。
    """
    if floor_to_minute:
        when = when.replace(second=0, microsecond=0)

    if not max_minutes or max_minutes <= 0:
        return when, 0

    shift = random.randint(0, int(max_minutes))
    return when + timedelta(minutes=shift), shift
