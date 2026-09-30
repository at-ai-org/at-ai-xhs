# -*- coding: utf-8 -*-
"""后台登录认证（at-ai-xhs 改造新增）。

整个 Web 侧（前端页面 + 全部后端接口）都要登录后才能用。这里放三件事：

1. **密码从哪来**：账号固定 `admin`；密码在**首次启动时随机生成一次**，
   明文写进 `settings.json` 的 `auth` 段并保持 600 权限，之后每次启动都沿用同一个。
   想换密码就直接改（或删掉）文件里 `auth.password`——删掉后下次启动会重新生成。
   生成的瞬间会往终端／`logs/backend.log` 打印一次，方便第一次部署时不用去翻文件。

2. **登录态怎么维持**：签发一个带签名的 token（itsdangerous），前端存 localStorage，
   之后每个请求带 `Authorization: Bearer xxx`。签名密钥 `auth.secret_key` 也在
   `settings.json` 里，随机生成一次。**删掉它 = 所有已登录会话立即失效**（需要重新登录），
   这是唯一能让"我怀疑 token 泄漏了"一键解决的开关。

3. **怎么挡住请求**：`install(app)` 注册一个全局 `before_request` 钩子。
   放在 app 层而不是逐个路由加装饰器，是因为接口散在三个文件里（`sau_backend.py`
   44 个路由 + 两个蓝图），逐个加一定会漏；以后新加接口也自动被保护。

为什么 SSE（`/login`、`/xhs/login/stream`）要单独说一句
------------------------------------------------------
`EventSource` 没法自定义请求头，token 只能挂在 URL 查询串上（`?token=...`）。
这是浏览器 API 的限制，不是设计选择。代价是 token 会出现在访问日志里——
本地单机工具可以接受；真要多机部署，应该改成"先换一次性 stream ticket 再连"。

为什么 `index.html` / `/assets/*` 是公开的
------------------------------------------
前端是个 SPA：登录页本身也是它的一部分。如果连页面外壳都要登录，
浏览器拿到的会是一段 401 JSON，登录页根本没机会加载出来——那就成了死循环。
所以公开的只有**页面外壳和静态资源**，所有**接口**（含素材、账号、发布、
模型设置、以及两个 SSE）一律要求登录，前端路由守卫把未登录的页面访问也挡在 `/login`。
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime
from typing import Any, Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from myUtils.app_settings import SETTINGS_PATH

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 固定账号名。密码随机，账号不随机——只有一个用户，没必要再造一个要记的东西。
USERNAME = "admin"

#: 密码长度 / 字符集。刻意去掉容易看错抄错的 0 O o 1 l I，
#: 因为密码是要人从终端或 JSON 文件里**抄到登录框**的。
PASSWORD_LENGTH = 16
PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"

#: token 有效期：7 天。本地工具，太短会烦人，太长又不好收拾。
TOKEN_MAX_AGE = 7 * 24 * 3600

_TOKEN_SALT = "at-ai-xhs-web-auth"

#: 登录失败限流：同一来源连续失败 N 次后冷却 M 秒。
#: 密码是 16 位随机串，暴力破解本来就不现实；这一层挡的是"顺手写个脚本试字典"。
LOCKOUT_THRESHOLD = 5
LOCKOUT_SECONDS = 30

_LOCK = threading.Lock()
_FAILED_ATTEMPTS: dict[str, list[float]] = {}


# --------------------------------------------------------------------------- #
# settings.json 的 auth 段读写
# --------------------------------------------------------------------------- #
# 注意：这里**不经过** app_settings.load_settings()／save_settings()。
#   读：load_settings() 会把 DEFAULT_SETTINGS 合并进来，而这里必须拿到文件原样内容，
#       否则每次保存都会把一堆默认值写进文件。
#   写：save_settings() 会主动剥掉 auth 段（防止有人通过模型设置接口改密码），
#       走它等于把刚生成的密码原样吞掉。


def _read_raw() -> dict:
    """读取 settings.json 原始内容。文件不存在／坏掉都退化成空 dict。"""
    if not SETTINGS_PATH.is_file():
        return {}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_raw(data: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(SETTINGS_PATH, 0o600)  # 里面有明文密码和 API Key
    except Exception:
        pass


def _new_password() -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


def _new_secret_key() -> str:
    return secrets.token_hex(32)


def ensure_auth() -> tuple[str, bool]:
    """保证 `auth` 段齐全：账号 / 密码 / 签名密钥。

    返回 `(password, generated)`——`generated=True` 表示这次是**新生成**的，
    调用方（启动流程）应该把它打印出来。已有密码时原样返回，绝不覆盖：
    密码只在"文件里没有"的时候生成一次。

    只在**确实缺东西**时才回写文件：每次启动都无条件重写会把用户手工调整过的
    格式／字段顺序抹掉，而这里大多数时候什么都不用做。
    """
    with _LOCK:
        data = _read_raw()
        auth = data.get("auth")
        if not isinstance(auth, dict):
            auth = {}

        generated = False
        changed = False

        if not (auth.get("password") or "").strip():
            auth["password"] = _new_password()
            auth["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            generated = True
            changed = True

        # 账号：允许被改，但为空时补回默认值
        if not (auth.get("username") or "").strip():
            auth["username"] = USERNAME
            changed = True

        # 签名密钥：没有就生成（已有则保留，否则每次重启都会把所有人踢下线）
        if not (auth.get("secret_key") or "").strip():
            auth["secret_key"] = _new_secret_key()
            changed = True

        if changed or data.get("auth") is not auth:
            data["auth"] = auth
            _write_raw(data)

        return auth["password"], generated


def bootstrap(verbose: bool = True) -> str:
    """启动时调用：补齐 auth 段，首次生成时把密码打到终端 / 日志里。"""
    password, generated = ensure_auth()
    if generated and verbose:
        print("=" * 68)
        print("🔐 已生成后台登录密码（明文保存在 settings.json 的 auth.password）")
        print(f"   账号：{get_username()}")
        print(f"   密码：{password}")
        print("   改密码：编辑 settings.json 的 auth.password，或删掉该字段后重启")
        print("=" * 68)
    return password


def get_username() -> str:
    return (get_auth_section().get("username") or USERNAME).strip() or USERNAME


def get_password() -> str:
    """读当前明文密码（仅供内部校验使用，**不要**往接口里回传）。"""
    return (get_auth_section().get("password") or "").strip()


def get_auth_section() -> dict:
    auth = _read_raw().get("auth")
    return auth if isinstance(auth, dict) else {}


# --------------------------------------------------------------------------- #
# 账号密码校验
# --------------------------------------------------------------------------- #


def verify_credentials(username: str, password: str) -> bool:
    """校验账号 + 密码。两边都用 `compare_digest`，避免按字符比较的时序差异。"""
    expected_user = get_username()
    expected_pass = get_password()
    if not expected_pass:
        # 理论上启动时已经补过；真走到这里说明 settings.json 被写坏了，
        # 这种情况下**拒绝**登录（fail closed），而不是放行。
        return False

    user_ok = hmac.compare_digest((username or "").encode("utf-8"), expected_user.encode("utf-8"))
    pass_ok = hmac.compare_digest((password or "").encode("utf-8"), expected_pass.encode("utf-8"))
    # 两个都比较完再合并，避免"用户名错就立刻返回"泄漏用户名是否存在
    return user_ok and pass_ok


# --------------------------------------------------------------------------- #
# 登录失败限流
# --------------------------------------------------------------------------- #


def lockout_remaining(client: str) -> float:
    """返回该来源还需等待的秒数；0 表示没被锁。"""
    now = time.time()
    with _LOCK:
        stamps = [t for t in _FAILED_ATTEMPTS.get(client, []) if now - t < LOCKOUT_SECONDS]
        if stamps:
            _FAILED_ATTEMPTS[client] = stamps
        else:
            _FAILED_ATTEMPTS.pop(client, None)
        if len(stamps) < LOCKOUT_THRESHOLD:
            return 0.0
        return max(0.0, LOCKOUT_SECONDS - (now - stamps[-1]))


def record_failure(client: str) -> None:
    with _LOCK:
        _FAILED_ATTEMPTS.setdefault(client, []).append(time.time())


def clear_failures(client: str) -> None:
    with _LOCK:
        _FAILED_ATTEMPTS.pop(client, None)


# --------------------------------------------------------------------------- #
# token 签发 / 校验
# --------------------------------------------------------------------------- #


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_current_secret(), salt=_TOKEN_SALT)


# 守卫在每个请求上都要验一次 token，而密钥就在 settings.json 里。
# 直接读文件的话每个请求都要 touch 一次磁盘——用 mtime 做键缓存住，
# 用户在外部手改了文件（换密钥 = 踢下线）也能立刻生效。
_secret_cache: Optional[tuple[float, str]] = None


def _current_secret() -> str:
    global _secret_cache

    try:
        mtime = SETTINGS_PATH.stat().st_mtime
    except OSError:
        mtime = 0.0

    if _secret_cache and _secret_cache[0] == mtime and _secret_cache[1]:
        return _secret_cache[1]

    secret = (get_auth_section().get("secret_key") or "").strip()
    if not secret:
        # 正常不会发生（bootstrap 会补）；补一次而不是抛异常，
        # 免得一个被手工清空的文件把整个后端变成 500。
        # 这里刻意**不写缓存**：刚回写过文件，mtime 已经变了，下一轮重读即可。
        ensure_auth()
        secret = (get_auth_section().get("secret_key") or "").strip()
        return secret

    _secret_cache = (mtime, secret)
    return secret


def issue_token(username: Optional[str] = None) -> str:
    return _serializer().dumps({"u": username or get_username()})


def verify_token(token: str) -> Optional[str]:
    """校验 token，返回用户名；无效 / 过期 / 被改过都返回 None。"""
    token = (token or "").strip()
    if not token:
        return None
    try:
        payload = _serializer().loads(token, max_age=TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return (payload.get("u") or "").strip() or None


def token_from(headers: Any, args: Any) -> str:
    """从请求里取 token。按优先级：请求头 → 查询串。

    查询串那条是给 `EventSource` 用的（它不能设请求头，见模块开头说明）。
    """
    auth_header = ""
    try:
        auth_header = (headers.get("Authorization") or "").strip()
    except Exception:
        auth_header = ""

    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    if auth_header:
        # 也接受直接放裸 token（自己写 curl / 脚本调试时省事）
        return auth_header

    for name in ("token", "access_token"):
        try:
            value = (args.get(name) or "").strip()
        except Exception:
            value = ""
        if value:
            return value

    try:
        return (headers.get("X-Auth-Token") or "").strip()
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Flask 集成
# --------------------------------------------------------------------------- #

#: 允许匿名访问的端点名。**只有页面外壳和静态资源**，没有任何业务接口。
PUBLIC_ENDPOINTS = {
    "auth.login",     # 登录接口本身
    "auth.status",    # 只回"有没有登录"，未登录时回 authenticated=false，供前端探活
    "index",          # GET /             → index.html（SPA 外壳，登录页在里面）
    "custom_static",  # GET /assets/<f>  → 前端 JS / CSS
    "favicon",        # GET /favicon.ico（内容其实是 logo.svg）
    "logo_svg",       # GET /logo.svg
    "static",         # Flask 内置 /static/<f>
}

PUBLIC_PREFIXES = ("/assets/",)


def _unauthorized(message: str = "未登录或登录已过期", code: int = 401):
    from flask import jsonify

    return jsonify({"code": code, "msg": message, "data": None}), code


def install(app, verbose: bool = True) -> None:
    """把认证装到 Flask app 上：全局守卫 + `/auth/*` 两个接口。"""
    from flask import Blueprint, request

    bootstrap(verbose=verbose)

    auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

    @auth_bp.post("/login")
    def login():
        from flask import jsonify

        client = request.remote_addr or "unknown"

        remaining = lockout_remaining(client)
        if remaining > 0:
            return _unauthorized(
                f"登录失败次数过多，请 {int(remaining) + 1} 秒后再试", code=429
            )

        body = request.get_json(silent=True) or {}
        username = body.get("username") or ""
        password = body.get("password") or ""

        if not verify_credentials(username, password):
            record_failure(client)
            left = LOCKOUT_THRESHOLD - len(_FAILED_ATTEMPTS.get(client, []))
            hint = f"，还可以试 {left} 次" if 0 < left < LOCKOUT_THRESHOLD else ""
            # 不区分"用户名错"还是"密码错"——这里只有一个账号，说细了没意义
            return _unauthorized(f"账号或密码不正确{hint}")

        clear_failures(client)
        return jsonify(
            {
                "code": 200,
                "msg": "登录成功",
                "data": {
                    "token": issue_token(username),
                    "username": get_username(),
                    "expires_in": TOKEN_MAX_AGE,
                },
            }
        )

    @auth_bp.get("/status")
    def status():
        """未登录也能调：只回答"当前 token 还有效吗"，不泄漏任何其他信息。"""
        from flask import jsonify

        username = verify_token(token_from(request.headers, request.args))
        return jsonify(
            {
                "code": 200,
                "msg": "ok",
                "data": {"authenticated": bool(username), "username": username or ""},
            }
        )

    app.register_blueprint(auth_bp)

    @app.before_request
    def _require_login():  # noqa: ANN202
        # CORS 预检不带 Authorization（浏览器规范如此），挡掉会让跨域请求全废
        if request.method == "OPTIONS":
            return None

        endpoint = request.endpoint or ""
        if endpoint in PUBLIC_ENDPOINTS:
            return None
        if any(request.path.startswith(p) for p in PUBLIC_PREFIXES):
            return None

        if verify_token(token_from(request.headers, request.args)):
            return None

        return _unauthorized()
