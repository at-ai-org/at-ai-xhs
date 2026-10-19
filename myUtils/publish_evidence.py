# -*- coding: utf-8 -*-
"""发布失败取证：**所有平台**的现场截图统一落在 `screenshots/<平台>/` 下。

为什么要统一
------------
发布中心是"平台 × 账号"并行跑的，一次失败可能同时来自五个平台。原来现场散在四个
地方，排查时要先去 `pdd_debug/`、再去 `ks_debug/`、再去仓库根目录找
`debug_tencent_*.png`、最后才想起小红书那条路在 `screenshots/` 平铺着——
而且抖音和快手那两个平铺目录里 100 多张图混在一起，光看文件名分不出是谁的。

现在的契约（只有两条，但都要守住）：

1. **目录只按平台分**：`screenshots/<平台>/`，平台名和 `sau_backend_publish.PLATFORMS`
   的 `business`、`logs/<平台>.log` 是同一套名字，
   不再有 `pdd_debug/`、`ks_debug/` 这种"按平台另起一个外层目录"的写法；
2. **取证绝不许把发布搞失败**：截图前 Playwright 要等 `document.fonts.ready`，
   机器忙时能一直撞到默认的 30 秒超时；取证是有界、落盘的副产物，不是发布的一环。
   所以这里的每个动作都自带超时、自带 try，异常只记日志。

一次真实事故就出在违反第 2 条上：2026-09-29 17:15:42 抖音那条，点「发布」后 3 秒
没跳转（并发跑着，是"慢"不是"坏"），进入重试分支截图 → 等字体等满 30 秒 → 异常从
`except` 冒出去，把本可以重试成功的发布写成了 `Page.screenshot: Timeout 30000ms
exceeded`（同一条任务第二轮 73 秒就发成功了）。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

#: 现场截图的根目录（相对仓库根）。`.gitignore` 里已忽略——它是排障产物，不入库。
EVIDENCE_ROOT = "screenshots"

#: 平台 → 子目录名。**必须**和 `sau_backend_publish.PLATFORMS[*]["business"]` 一致
#: （有测试钉着，见 `tests/test_publish_evidence.py`）。
PLATFORM_DIRS: dict[str, str] = {
    "xiaohongshu": "xiaohongshu",
    "tencent": "tencent",
    "douyin": "douyin",
    "kuaishou": "kuaishou",
    "pdd": "pdd",
    # 人工验收脚本（tools/verify_*.py）的截图：不是发布现场，单独放一处，
    # 免得和"失败现场"混在一起看。
    "tools": "tools",
}

#: 取证截图的默认超时。5 秒足够一张全页截图，又不值得让发布多等。
DEFAULT_SHOT_TIMEOUT_MS = 5_000


def evidence_root() -> Path:
    """现场根目录（`<仓库根>/screenshots`）。

    以仓库根为基准而不是当前工作目录：后端从 `start.sh` 启动时 cwd 是仓库根，
    但从别的目录跑 CLI / 脚本时不是——那时截图会落到一个谁也找不到的地方。
    """
    from conf import BASE_DIR

    return Path(BASE_DIR) / EVIDENCE_ROOT


def platform_dir(platform: str, *, create: bool = True) -> Path:
    """`screenshots/<平台>/`；默认顺手建出来，返回绝对路径。

    `platform` 必须是 `PLATFORM_DIRS` 里的键。写错名字要**立刻报错**，不能悄悄
    落一个 `screenshots/unknown/`——那种目录排查时看见只会更糊涂。
    """
    if platform not in PLATFORM_DIRS:
        raise ValueError(
            f"未知的取证平台 {platform!r}，只能取 {sorted(PLATFORM_DIRS)} 之一"
        )
    path = evidence_root() / PLATFORM_DIRS[platform]
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def dir_hint(platform: str) -> str:
    """给用户看的相对路径（用在错误信息里，例如"现场已存到 screenshots/pdd/"）。"""
    return f"{EVIDENCE_ROOT}/{PLATFORM_DIRS[platform]}/"


def safe_tag(tag: str) -> str:
    """文件名安全化：路径分隔符等一律换成 `_`（中文保留，它本来就是合法文件名）。"""
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(tag or ""))
    return cleaned.strip("._") or "shot"


def _reserve(path: Path) -> Path:
    """原子占坑，返回本次真正能用的路径（同一秒、同平台、同标签时不会互相覆盖）。

    为什么不是"先看文件在不在、不在就用"：发布中心是"平台 × 账号"并行跑的，
    **同一条素材发给同一个平台的三个账号**很常见，三条任务在同一秒里一起失败时，
    那个"查一次再写"的窗口里三条线程都会认为文件名可用，最后只剩一张图。
    这里用 `O_CREAT|O_EXCL` 让内核来判先后：建不出来就换个带短后缀的名字再试。
    """
    candidate = path
    for _ in range(50):
        try:
            os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return candidate
        except FileExistsError:
            candidate = candidate.with_name(
                f"{candidate.stem}-{uuid.uuid4().hex[:4]}{candidate.suffix}"
            )
    return candidate


async def capture(
    page: Any,
    platform: str,
    tag: str,
    *,
    logger: Any = None,
    html: bool = False,
    timeout_ms: int = DEFAULT_SHOT_TIMEOUT_MS,
    full_page: bool = True,
) -> str:
    """落一张现场全页截图（`html=True` 时再附一份同名 HTML），返回 PNG 路径。

    **这个函数自己绝不允许把发布搞失败**：内部每个动作都有独立 try，失败只记一条
    日志并返回空串，调用方继续抛它自己的错。`page` 为 None（还没开浏览器就失败了）
    时同样只记日志——那种情况本来也没有页面可拍。

    返回的是**绝对路径**；给用户看的相对路径用 `dir_hint()`。
    """
    log = logger
    if page is None:
        return ""
    try:
        directory = platform_dir(platform)
        stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
        path = _reserve(directory / f"{stamp}-{safe_tag(tag)}.png")

        if html:
            try:
                path.with_suffix(".html").write_text(await page.content(), encoding="utf-8")
            except Exception as exc:  # noqa: BLE001 —— 存 HTML 失败不该影响截图
                _warn(log, f"存 HTML 失败（不影响发布）: {exc}")

        try:
            await page.screenshot(path=str(path), full_page=full_page, timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001 —— 截图失败绝不能影响发布
            # 占坑用的空文件要删掉：留着 0 字节的 .png 比"什么都没有"更误导人。
            try:
                path.unlink()
            except OSError:
                pass
            _warn(log, f"截图失败（不影响发布继续）: {exc}")
            return ""

        _warn(log, f"已保存现场: {path}")
        return str(path)
    except Exception as exc:  # noqa: BLE001
        _warn(log, f"保存现场失败（不影响发布）: {exc}")
        return ""


def describe(platform: str, tag: str = "") -> str:
    """错误信息里那句"现场已存到 …"。

    单独抽出来是为了让每条失败路径都能带上它——用户看到失败结论的**同一个地方**
    就知道去哪儿找截图，不用回头翻日志。
    """
    hint = f"现场已存到 {dir_hint(platform)}"
    return f"{hint}（{tag}）" if tag else hint


def _warn(log: Any, text: str) -> None:
    message = f"📸 {text}"
    if log is not None:
        try:
            log.warning(message)
            return
        except Exception:  # noqa: BLE001
            pass
    print(message)


__all__ = [
    "DEFAULT_SHOT_TIMEOUT_MS",
    "EVIDENCE_ROOT",
    "PLATFORM_DIRS",
    "capture",
    "describe",
    "dir_hint",
    "evidence_root",
    "platform_dir",
    "safe_tag",
]
