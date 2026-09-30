# -*- coding: utf-8 -*-
"""账号会话定时续期的 Web 接口（at-ai-xhs 改造新增）。

**为什么单独一个文件、而不是挂在 `/xhs` 下**：续期的对象是「账号管理」里的
**全部账号**（快手 / 抖音 / 视频号 / 小红书），跟小红书没有半点关系。挂在 `/xhs`
前缀下会让人以为只续小红书——这类前缀在排查问题时会真的把人带偏，所以这里
按"功能"开一条自己的路径：

    GET  /session-renew    当前状态（开关、时间点、上次结果、下次时间）
    POST /session-renew    改开关 / 改时间点

真正干活的是 `myUtils/session_renew.py`（后端自己的调度线程），这里只是读写配置 + 回显状态。
落盘还是走 `myUtils/app_settings.py` 的 `save_settings()`，也就是 settings.json 的
`session_renew` 段——和「设置」页共用同一处配置。
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from myUtils import session_renew
from myUtils.app_settings import normalize_renew_times, save_settings

session_bp = Blueprint("session", __name__)


def _ok(data=None, msg="ok"):
    return jsonify({"code": 200, "success": True, "msg": msg, "data": data})


def _err(msg: str, code: int = 500, data=None):
    return jsonify({"code": code, "success": False, "msg": str(msg), "data": data}), code


@session_bp.get("/session-renew")
def session_renew_status():
    """当前状态。

    「账号管理」和「设置」两页上的那个「定时续期」按钮都靠它渲染
    （开关状态、时间点、上次续期结果、下次什么时候跑）。
    """
    return _ok(session_renew.status())


@session_bp.post("/session-renew")
def session_renew_save():
    """改开关 / 改时间点。

    专门开一个接口而不是复用 `POST /xhs/settings`，是因为这个按钮在两页上都出现
    （「账号管理」和「设置」），点一下就该立刻生效并回显新状态——没必要让前端
    把整份设置（含模型配置）凑齐了再提交。

    body: `{"enabled": bool?, "times": ["00:00", ...]?}`，两个都可选，只传哪个改哪个。
    """
    body = request.get_json(silent=True) or {}
    patch: dict = {}

    if "enabled" in body:
        patch["enabled"] = bool(body.get("enabled"))
    if "times" in body:
        times = normalize_renew_times(body.get("times"))
        if not times:
            # 空时间点 = 开关显示"开启"但一个点都不跑，界面上完全看不出来。
            # 想停就关开关，所以这里直接拒绝，而不是默默存一个空列表。
            return _err("至少需要一个有效的时间点（格式 HH:MM，例如 07:00）", 400)
        patch["times"] = times
    if not patch:
        return _err("没有要修改的内容（enabled / times 至少给一个）", 400)

    try:
        save_settings({"session_renew": patch})
    except Exception as exc:
        return _err(f"保存续期设置失败: {exc}")
    return _ok(session_renew.status(), "已保存")


__all__ = ["session_bp"]
