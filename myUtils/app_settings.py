# -*- coding: utf-8 -*-
"""应用设置（at-ai-xhs 改造新增）。

目前只存一份「OpenAI 兼容格式」的模型配置，供视频理解 / 文案生成使用。
存成项目内的 JSON 文件，不写数据库——配置是"人改的"，用文件最直观，也方便直接查看和备份。

安全提醒
--------
API Key 以**明文**存在 `model_settings.json`（权限 600）。这是本地单机工具，
但请知悉：不要把该文件提交到版本库（已在 .gitignore 里排除）。
对外接口一律只返回掩码，不返回明文 key。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from conf import BASE_DIR

SETTINGS_PATH = Path(BASE_DIR) / "model_settings.json"

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
    # 成片确实含 AI 生成内容（TTS 配音 / AI 画面）时，在「模型设置 → 发布默认值」
    # 里填上，例如 "含AI生成内容"。
    "tencent_content_label": "",
    # 抖音发布页的「自主声明」（单选，无二级选项、无需填来源）。
    # **默认留空 = 不声明**：实拍视频没有 AI 生成内容，平台也不强制；
    # 以前上传器会无条件兜底选「内容由AI生成」，等于给实拍视频挂了个 AI 牌子。
    # 成片确实含 AI 生成内容时再在「模型设置 → 发布默认值」里填上。
    "douyin_declaration": "",
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


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
    """
    current = load_settings()
    incoming = json.loads(json.dumps(patch or {}))

    vision_patch = incoming.get("vision") or {}
    if vision_patch.get("api_key") == "" or vision_patch.get("api_key") is None:
        vision_patch.pop("api_key", None)
        incoming["vision"] = vision_patch

    merged = _deep_merge(current, incoming)
    merged["vision"]["api_key"] = (merged["vision"].get("api_key") or "").strip()
    merged["vision"]["base_url"] = (merged["vision"].get("base_url") or "").strip().rstrip("/")
    merged["vision"]["model"] = (merged["vision"].get("model") or "").strip()

    SETTINGS_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(SETTINGS_PATH, 0o600)  # 里面有 key，收一下权限
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
    """给前端的版本：key 只给掩码 + 是否有 key 的布尔值。"""
    s = load_settings()
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
