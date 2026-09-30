# -*- coding: utf-8 -*-
"""应用设置（at-ai-xhs 改造新增）。

目前存一份「OpenAI 兼容格式」的模型配置，供视频理解 / 文案生成使用，
外加发布默认值（合集 / 视频号标注 / 抖音声明）和账号会话定时续期（`session_renew`，
执行逻辑见 myUtils/session_renew.py）。
存成项目内的 JSON 文件，不写数据库——配置是"人改的"，用文件最直观，也方便直接查看和备份。

同一个 `settings.json` 里还有 `auth` 段（后台登录账号 + 随机密码），**由
`myUtils/web_auth.py` 独立读写**——它不经过这里的 `save_settings()`，
所以本模块必须主动把它挡住（见下面 `_strip_auth` 的说明）。

安全提醒
--------
API Key 和后台登录密码都以**明文**存在 `settings.json`（权限 600）。这是本地单机工具，
但请知悉：不要把该文件提交到版本库（已在 .gitignore 里排除）。
对外接口一律只返回掩码，**不回传 auth 段**。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from conf import BASE_DIR

SETTINGS_PATH = Path(BASE_DIR) / "settings.json"

#: 会话续期的默认时间点：0 点 / 7 点 / 14 点 / 20 点。
#:
#: 为什么是这四个：平台的登录态是**服务端会话**，本地 cookie 文件上的 expires
#: （动辄写到一年后）跟它什么时候被作废没关系，唯一的办法是定期真的去用一次。
#: 四个点大致覆盖"半夜 / 早上 / 下午 / 晚上"四段，间隔不超过 10 小时，
#: 又不会像每小时一次那样把浏览器开得太频繁（每个平台一次校验要几十秒）。
DEFAULT_SESSION_RENEW_TIMES: tuple[str, ...] = ("00:00", "07:00", "14:00", "20:00")

#: 时间点数量上限。纯属防呆：一天 24 小时，多出来的没有意义，
#: 而且每个点都要开一轮浏览器，配错了会把机器占满。
MAX_SESSION_RENEW_TIMES = 24

#: 时间点接受的写法：`7:00` / `07:00` 都行，`0700`、`7点` 这类不认（宁可报错也别猜）。
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{1,2})$")

DEFAULT_SETTINGS: dict[str, Any] = {
    "vision": {
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "enabled": True,
        # 生成文案时的额外要求（可留空）
        "extra_prompt": "",
        "timeout": 120,
        # 推理模型（会输出 reasoning_tokens 的那种）思考过程也占这个额度，
        # 给太小会导致正式输出被截断——JSON 写到一半就没了。
        "max_tokens": 8000,
        # 生成正文的字数上限（小红书正文本身可到 1000 字，这里是我们主动收窄）
        "desc_max_chars": 100,
    },
    # 发布时默认选中的合集名（留空表示不自动选）
    "default_collection": "风景",
    # 视频号发布页的「视频标注」（下拉里与「内容为转载」等并列）。
    # **默认留空 = 不设置这项标注**：实拍视频没有 AI 生成内容，平台也不强制，
    # 硬点这一项只会白等 5 秒超时（见 uploader/tencent_uploader/main.py）。
    # 成片确实含 AI 生成内容（TTS 配音 / AI 画面）时，在「设置 → 发布默认值」
    # 里填上，例如 "含AI生成内容"。
    "tencent_content_label": "",
    # 抖音发布页的「自主声明」（单选，无二级选项、无需填来源）。
    # **默认留空 = 不声明**：实拍视频没有 AI 生成内容，平台也不强制；
    # 以前上传器会无条件兜底选「内容由AI生成」，等于给实拍视频挂了个 AI 牌子。
    # 成片确实含 AI 生成内容时再在「设置 → 发布默认值」里填上。
    "douyin_declaration": "",
    # 账号会话定时续期（「账号管理」和「设置」页上那个「定时续期」按钮）。
    # **默认开启**：这是在防"账号悄悄掉线"——平台的服务端会话不用就会被作废，
    # 而本地 cookie 文件上的过期时间看不出来。到点后的动作见 myUtils/session_renew.py。
    "session_renew": {
        "enabled": True,
        "times": list(DEFAULT_SESSION_RENEW_TIMES),
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _strip_auth(data: dict) -> dict:
    """摘掉 `auth` 段（后台登录账号 / 密码）。

    两个地方必须挡：
      * `save_settings()` —— 它接受前端传来的任意 JSON，不挡的话任何人都能用
        `POST /xhs/settings {"auth": {...}}` 直接改登录密码（或者塞一个自己的密码进去）。
        登录接口本身是要认证的，但"能通过 settings 接口写密码"仍然是不该开的口子。
      * `public_settings()` —— 不挡的话模型设置接口会把明文密码回传给浏览器。
    """
    out = dict(data or {})
    out.pop("auth", None)
    return out


def _merge_session_renew(current: Any, patch: Any) -> dict:
    """把续期配置的入参并到当前值上，并归一化成一个干净的 dict。

    两种容错，都是为了让"配置坏了"不要变成"静默不续期"：

    * `patch` 不是 dict（有人写 `{"session_renew": true}`）时，当成只改开关；
    * `times` 归一化后为空（前端传了空数组、或者全是认不出来的写法）时**保留原值**，
      而不是把时间点清空——清空后开关还显示"开启"，但一个点都不跑，
      界面上完全看不出来。真要停，请把 `enabled` 关掉。
    """
    base = dict(DEFAULT_SETTINGS["session_renew"])
    if isinstance(current, dict):
        base.update(current)

    incoming = patch
    if not isinstance(incoming, dict) and incoming is not None:
        incoming = {"enabled": bool(incoming)}
    incoming = incoming or {}

    if "enabled" in incoming:
        base["enabled"] = bool(incoming.get("enabled"))
    else:
        base["enabled"] = bool(base.get("enabled", True))

    if "times" in incoming:
        times = normalize_renew_times(incoming.get("times"))
        if times:
            base["times"] = times
    times = normalize_renew_times(base.get("times"))
    base["times"] = times or list(DEFAULT_SESSION_RENEW_TIMES)
    return base


def normalize_renew_times(raw: Iterable[Any] | str | None) -> list[str]:
    """把"时间点"收拾成 `["00:00", "07:00", ...]`（升序、去重、两位小时）。

    为什么要归一化而不是直接信前端：时间点会被**调度器按字符串比较**当前时刻
    （`now.strftime("%H:%M") in times`）。只要有一处是 `"7:00"` 这种写法，
    那个点就永远不会命中——而且是静默的，界面上还显示"已开启"。
    所以入口（save_settings / 续期接口）统一过一遍这里。

    认的写法：`"7:00"` / `"07:00"` / `" 20:30 "`；也接受逗号或空格分隔的一整串。
    不认的（`"0700"`、`"7点"`、越界的小时/分钟）直接丢掉——宁可少一个点，
    也不要猜出一个用户没想到的时间。
    """
    items: list[Any]
    if raw is None:
        items = []
    elif isinstance(raw, str):
        items = [p for p in re.split(r"[,，\s]+", raw) if p]
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = [raw]

    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        m = _TIME_RE.match(text)
        if not m:
            continue
        hour, minute = int(m.group(1)), int(m.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            continue
        value = f"{hour:02d}:{minute:02d}"
        if value not in out:
            out.append(value)
    out.sort()
    return out[:MAX_SESSION_RENEW_TIMES]


def load_settings() -> dict:
    """读取设置，缺失字段用默认值补齐（便于后续加字段）。"""
    if not SETTINGS_PATH.is_file():
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    return _deep_merge(DEFAULT_SETTINGS, raw)


def save_settings(patch: dict) -> dict:
    """合并保存。

    约定：`vision.api_key` 传空字符串表示"不修改"（前端不回显明文 key，
    所以保存时不该把 key 抹掉）。

    `auth` 段（登录账号 / 密码）由 `myUtils/web_auth.py` 独占管理，
    这里的入参一律丢弃它，避免有人通过设置接口改掉登录密码。
    """
    current = load_settings()
    incoming = _strip_auth(json.loads(json.dumps(patch or {})))

    vision_patch = incoming.get("vision") or {}
    if vision_patch.get("api_key") == "" or vision_patch.get("api_key") is None:
        vision_patch.pop("api_key", None)
        incoming["vision"] = vision_patch

    merged = _deep_merge(current, incoming)
    merged["vision"]["api_key"] = (merged["vision"].get("api_key") or "").strip()
    merged["vision"]["base_url"] = (merged["vision"].get("base_url") or "").strip().rstrip("/")
    merged["vision"]["model"] = (merged["vision"].get("model") or "").strip()

    merged["session_renew"] = _merge_session_renew(
        current.get("session_renew"), incoming.get("session_renew")
    )

    SETTINGS_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(SETTINGS_PATH, 0o600)  # 里面有 key 和登录密码，收一下权限
    except Exception:
        pass
    return merged


def mask_key(key: str) -> str:
    """只回显尾部，避免前端拿到完整 key。"""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * 6}{key[-4:]}"


def public_settings() -> dict:
    """给前端的版本：key 只给掩码 + 是否有 key 的布尔值。

    顺带摘掉 `auth` 段——模型设置页不需要它，而它里面是**明文登录密码**。
    """
    s = _strip_auth(load_settings())
    vision = dict(s.get("vision") or {})
    raw_key = vision.pop("api_key", "") or ""
    vision["api_key_masked"] = mask_key(raw_key)
    vision["has_key"] = bool(raw_key)
    s["vision"] = vision
    return s


def get_vision_config() -> dict:
    """给调用方用的真实配置（含明文 key）。"""
    return dict(load_settings().get("vision") or {})


def get_default_collection() -> str:
    return (load_settings().get("default_collection") or "").strip()


def get_tencent_content_label() -> str:
    """视频号要选的「视频标注」文案；**空字符串表示不设置这项标注**。"""
    return (load_settings().get("tencent_content_label") or "").strip()


def get_douyin_declaration() -> str:
    """抖音要选的「自主声明」文案；**空字符串表示不做任何声明**。"""
    return (load_settings().get("douyin_declaration") or "").strip()


def get_session_renew_config() -> dict:
    """会话续期配置（调度器每次 tick 都读一遍，改完设置**不用重启后端**就生效）。

    返回的 `times` / `enabled` 一定是归一化后的值，调用方可以拿它直接比较/遍历。
    """
    merged = _merge_session_renew(load_settings().get("session_renew"), None)
    return {"enabled": merged["enabled"], "times": list(merged["times"])}
