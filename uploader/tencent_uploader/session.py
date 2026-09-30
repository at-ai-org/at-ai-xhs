# -*- coding: utf-8 -*-
"""视频号（channels.weixin.qq.com）会话与浏览器 profile 的统一入口（at-ai-xhs 改造新增）。

为什么要有这个文件
------------------
上游 social-auto-upload 对登录态只有一套做法：「扫码 → 存一份 storage_state JSON →
每次任务 `new_context(storage_state=…)` → 用完（而且只在发布成功那条路上）把 JSON 覆盖回去」。
327 个提交里从没出现过持久化 profile 或任何续期逻辑（`git log --all -S user_data_dir` = 0）。

本项目实测的后果：9-25 20:20 扫码的号，当晚 22:30 校验还有效，第二天 08:19 服务端会话
已被作废，而本地 cookie 文件里的 expires 还写着 2027-10-31——**文件本身一无所知**。

这里补三件上游没做的事：

1. **每条路径都回写 cookie**（校验 / 失败 / 异常一律回写），服务端在会话期间下发的任何
   新票据都不会丢（上游只在发布成功那一行回写）；
2. **持久化 profile 取代一次性 context**：cookie 罐、localStorage、IndexedDB 都留在
   `cookies/tencent_uploader/<uin>/`，登录、校验、发布是同一个「设备」；目录按
   **微信号（wxuin）** 命名，所以重新扫码也是回到同一台设备上，而不是又冒出一台新的；
3. **登录 / 校验 / 发布共用一套启动参数**（同一份 patchright 配置、同一个无头开关），
   不再出现「同一张票在三种指纹下使用」。

两个容易踩的点
--------------
* 客户端 cookie 的 `expires` 和服务端会话存活期是两件事，所以判断死活只能真开浏览器试，
  不能读文件推断；
* 这版 playwright/patchright 的 `launch_persistent_context` **不接受 `storage_state`** 参数，
  所以「把账号 JSON 灌进 profile」只能起完浏览器再 `add_cookies`。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import conf as _conf
from myUtils.browser_lock import LOCK_WAIT_TIMEOUT
from myUtils.browser_lock import BrowserLockTimeout
from myUtils.browser_lock import account_lock
from utils.base_social_media import set_init_script
from utils.log import tencent_logger

# conf.py 是**每个环境各一份**、不进版本库的（Dockerfile 里就是 `cp conf.example.py conf.py`），
# 所以这里取新开关时留了兜底：老 conf.py 没写这两个字段也能跑，不会抛 ImportError。
LOCAL_CHROME_PATH = _conf.LOCAL_CHROME_PATH
TENCENT_HEADLESS = bool(getattr(_conf, "TENCENT_HEADLESS", _conf.LOCAL_CHROME_HEADLESS))
TENCENT_PROFILE_ROOT = getattr(_conf, "TENCENT_PROFILE_ROOT", _conf.BASE_DIR / "cookies" / "tencent_uploader")

#: 收尾动作（回写 cookie / 关闭浏览器）各自的超时秒数。
#:
#: 为什么必须有：这两个动作跑在 `with lock:` **里面**，而这个锁就是"同一个 profile
#: 同时只能有一个 Chrome"的保证。一旦 `context.close()` 不返回（实测过：Chromium
#: 内核已经没了、但 patchright 驱动进程还活着，close 就一直 await 下去），锁就永远
#: 不释放——之后这个账号的发布任务会全部卡在等锁上，既不报错也不超时，界面上表现为
#: "任务一直卡着"。加超时的代价是可能留下一个没关干净的浏览器，但比一个账号永久
#: 卡死要轻得多。
CLEANUP_TIMEOUT = float(os.environ.get("SAU_TENCENT_CLEANUP_TIMEOUT", "30") or 30)

CHANNELS_DOMAIN = "channels.weixin.qq.com"
#: 扫码登录成功后真正代表登录态的 cookie。用它来判断账号文件有没有指向某个微信号。
SESSION_COOKIE = "sessionid"
UIN_COOKIE = "wxuin"

#: 登录用的临时 profile 前缀（登录成功后再归位到 uin_<wxuin>）。
STAGING_PREFIX = "_login_"

#: Chrome 的单例锁文件。被强杀（Ctrl-C / 后端重启）时会留下，不清掉下次起不来。
_SINGLETON_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


# --------------------------------------------------------------------------- #
# 启动参数：登录 / 校验 / 发布共用这一份
# --------------------------------------------------------------------------- #
def build_launch_kwargs(headless: bool | None = None) -> dict:
    """持久化 context 的启动参数（唯一来源，别再各写一份）。

    按 patchright 官方「Best Practice - use Chrome without Fingerprint Injection」来：
    真 Chrome（channel="chrome"）+ 真实窗口尺寸（no_viewport），**不自加 UA / headers**
    （那才是明显的指纹注入）。patchright 会自己补
    `--disable-blink-features=AutomationControlled`、去掉 `--enable-automation`，
    所以这里不再手写这些参数。
    """
    headless = TENCENT_HEADLESS if headless is None else bool(headless)
    kwargs: dict = {
        "headless": headless,
        # patchright 文档建议：持久化 profile 用 no_viewport，走真实窗口尺寸
        "no_viewport": True,
    }
    if not headless:
        # 有头（A/B）时把窗口最大化：不然新 profile 的窗口可能很小，
        # 视频号发布页的元素会被挤到可视区外，点不到。这也是本项目其它平台
        # 登录用的常规参数（myUtils/login.py: get_browser_options）。
        kwargs["args"] = ["--start-maximized"]
    if LOCAL_CHROME_PATH:
        kwargs["executable_path"] = LOCAL_CHROME_PATH
    else:
        kwargs["channel"] = "chrome"
    return kwargs


#: 只有 context 认、`launch()` 不认的参数。降级探测走的是
#: `launch()` + `new_context()` 两步，必须把它们分开传，否则
#: `launch()` 会直接抛 "unexpected keyword argument 'no_viewport'"。
_CONTEXT_ONLY_KEYS = ("no_viewport",)


def build_ephemeral_kwargs(headless: bool | None = None) -> tuple[dict, dict]:
    """一次性 context（profile 被占用时的降级探测）的 (launch 参数, new_context 参数)。"""
    kwargs = build_launch_kwargs(headless)
    launch_kwargs = {k: v for k, v in kwargs.items() if k not in _CONTEXT_ONLY_KEYS}
    context_kwargs = {k: v for k, v in kwargs.items() if k in _CONTEXT_ONLY_KEYS}
    return launch_kwargs, context_kwargs


# --------------------------------------------------------------------------- #
# profile 目录：按微信号命名，登录 / 校验 / 发布都从这里取
# --------------------------------------------------------------------------- #
def profile_root() -> Path:
    root = Path(TENCENT_PROFILE_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return root


def read_uin(account_file: str | Path) -> str | None:
    """从账号 JSON 里读出微信号（wxuin）。profile 目录就是按它命名的。"""
    path = Path(account_file).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    for cookie in data.get("cookies") or []:
        if str(cookie.get("name") or "") != UIN_COOKIE:
            continue
        if CHANNELS_DOMAIN not in str(cookie.get("domain") or ""):
            continue
        value = str(cookie.get("value") or "").strip()
        if value:
            return value
    return None


def profile_dir_for(account_file: str | Path) -> Path:
    """账号对应的持久化 profile 目录。

    优先 `uin_<wxuin>`：同一个微信号无论扫码多少次，用的都是同一个目录，设备身份连续。
    读不到 wxuin（老文件 / 文件损坏）时退回按文件名区分，保证不会因为读不出来就写错地方。
    """
    path = Path(account_file).expanduser()
    uin = read_uin(path)
    return profile_root() / (f"uin_{uin}" if uin else f"file_{path.stem}")


def new_staging_dir() -> Path:
    _sweep_stale_staging()
    return profile_root() / f"{STAGING_PREFIX}{uuid.uuid4().hex[:12]}"


def _sweep_stale_staging(max_age_hours: float = 6) -> None:
    """清掉以前被强杀留下的临时登录 profile。

    正常路径下 `finalize_login_profile()` 会把它归位、登录失败的路径会删掉它，
    但进程被 Ctrl-C / 后端重启打断时就只剩一个目录躺在那儿（几十 MB）。分配新临时目录时
    顺手扫一遍：只有前缀是登录用的、而且**超过 max_age_hours 没动过**的才删——正在扫码的
    会话最长也就几分钟，留 6 小时余量不会误伤。
    """
    root = Path(TENCENT_PROFILE_ROOT)
    if not root.exists():
        return
    cutoff = time.time() - max_age_hours * 3600
    for candidate in root.glob(f"{STAGING_PREFIX}*"):
        try:
            if candidate.is_dir() and candidate.stat().st_mtime < cutoff:
                shutil.rmtree(candidate, ignore_errors=True)
                tencent_logger.info(_msg("🧹", f"清掉过期的临时登录 profile: {candidate.name}"))
        except Exception:
            continue


def discard_profile(profile_dir: str | Path | None) -> None:
    """删掉一个 profile 目录（登录失败要清临时目录时用）。"""
    if not profile_dir:
        return
    path = Path(profile_dir)
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except Exception:
        # 没权限判断（别人的进程）就当它活着，宁可不删
        return True
    return True


def cleanup_stale_lock(profile_dir: str | Path) -> None:
    """清掉上次被强杀留下的 Chrome 单例锁。

    发布任务被 Ctrl-C / 后端重启打断是常态（本项目的 `publish_records` 里就有
    "后端进程重启，任务未能跑完"这种记录），profile 目录会留下 SingletonLock，
    下次启动 Chrome 会直接报 "profile is already in use"。
    Chrome 自己能判断锁是不是 stale，但只认 symlink 里的 pid——这里先看一眼，
    pid 已经不在了就删，pid 还活着就原样留给 Chrome 自己报错。
    """
    base = Path(profile_dir)
    lock = base / "SingletonLock"
    try:
        if not lock.is_symlink() and not lock.exists():
            return
        target = os.readlink(lock) if lock.is_symlink() else ""
        pid_text = str(target).rsplit("-", 1)[-1]
        if pid_text.isdigit() and _pid_alive(int(pid_text)):
            return
        for name in _SINGLETON_FILES:
            try:
                (base / name).unlink(missing_ok=True)
            except Exception:
                pass
        tencent_logger.info(_msg("🧹", f"清掉上次残留的浏览器锁: {base.name}"))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# cookie 进 / 出
# --------------------------------------------------------------------------- #
def _load_cookies(account_file: Path) -> list[dict]:
    data = json.loads(account_file.read_text(encoding="utf-8"))
    cookies: list[dict] = []
    for raw in data.get("cookies") or []:
        if not raw.get("name") or "value" not in raw:
            continue
        cookie = {
            key: raw[key]
            for key in ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
            if key in raw
        }
        cookies.append(cookie)
    return cookies


async def seed_cookies(context, account_file: str | Path) -> int:
    """把账号 JSON 里的 cookie 灌进持久化 profile。

    规则是「JSON 是唯一权威」：同名 cookie 直接覆盖。这样重新扫码之后新 JSON 一启动就
    生效，不会出现「profile 里躺着一个旧的 sessionid 把新登录顶掉」这种查不出来的毛病。
    profile 里其它 cookie（其它域的设备票据等）不受影响，继续留着。
    """
    path = Path(account_file).expanduser()
    if not path.exists():
        return 0
    try:
        cookies = _load_cookies(path)
    except Exception as exc:
        tencent_logger.warning(_msg("⚠️", f"读取账号 cookie 文件失败，跳过注入: {exc}"))
        return 0
    if not cookies:
        return 0
    try:
        await context.add_cookies(cookies)
        return len(cookies)
    except Exception as exc:
        tencent_logger.warning(_msg("⚠️", f"cookie 注入失败（继续用 profile 里已有的状态）: {exc}"))
        return 0


async def save_storage_state(context, account_file: str | Path) -> bool:
    """回写账号 JSON。**每条路径都要调用**（成功、失败、异常都要）。

    带一道保护：context 里一个 cookie 都没有时不写——那通常意味着浏览器刚起来就崩了，
    真写下去会把原本可用的账号文件清空，比不写更糟。
    """
    path = Path(account_file).expanduser()
    try:
        cookies = await context.cookies()
    except Exception as exc:
        tencent_logger.warning(_msg("⚠️", f"读取浏览器 cookie 失败，跳过回写: {exc}"))
        return False
    if not cookies:
        tencent_logger.warning(_msg("⚠️", "浏览器里没有任何 cookie，跳过回写（避免把账号文件写空）"))
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(path))
        return True
    except Exception as exc:
        tencent_logger.warning(_msg("⚠️", f"cookie 回写失败: {exc}"))
        return False


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
@dataclass
class TencentSession:
    """一次视频号浏览器会话。`profile_dir` 在登录流程里还要用（归位 / 失败清理）。"""

    context: object
    profile_dir: Path
    account_file: Path | None
    seeded: int = 0
    #: True = 这次是「profile 被占用，降级成一次性 context」的只读探测
    ephemeral: bool = False


@asynccontextmanager
async def tencent_session(
    playwright,
    account_file=None,
    *,
    headless: bool | None = None,
    profile_dir=None,
    seed: bool = True,
    persist: bool = True,
    fallback_when_busy: bool = False,
):
    """开一次视频号浏览器会话，登录 / 校验 / 发布都走这里。

    * `account_file` 给了且 `seed=True` → 用该账号的持久化 profile，并把 JSON 里的
      cookie 注入进去；
    * `seed=False`（登录用）→ 用一个全新的临时 profile，登录成功后由
      `finalize_login_profile()` 归位到 `uin_<wxuin>`；
    * 退出时（无论成功、失败还是抛异常）都会把 cookie 回写 `account_file`；
    * `fallback_when_busy=True`（只读探测用，比如账号管理页的校验）→ 这个 profile
      正被别的任务占用时**不排队等**，直接降级成一次性 context 去看一眼登录态。
      没有这条的话：发布任务占着 profile 要几分钟，而账号管理页每次刷新都会逐账号校验，
      页面就会干等几分钟（以前不用 profile，没有这个问题）。

    锁的粒度是 **profile 目录**而不是账号文件：两个指向同一个微信号的账号文件会落进同一个
    profile 目录，而一个 profile 目录同时只能有一个 Chrome 在用。
    """
    account_path = Path(account_file).expanduser() if account_file else None
    if profile_dir is not None:
        target = Path(profile_dir)
    elif account_path is not None and seed:
        target = profile_dir_for(account_path)
    else:
        target = new_staging_dir()

    target.mkdir(parents=True, exist_ok=True)
    cleanup_stale_lock(target)

    # 抢锁**只能有一次**：`acquire(blocking=False)` 不是"探测"，成功时它真的把锁拿走了。
    #
    # 这里以前写的是 `if fallback_when_busy and not lock.acquire(blocking=False):` —— 锁空闲
    # 时那次 acquire 成功、条件为假，代码继续往下**又 acquire 一次**（计数 2），而收尾只
    # `release()` 一次（计数停在 1）。`RLock` 同线程可重入、跨线程互斥，于是：
    #   * 泄漏它的那条任务线程自己还能再进去 → "重启后端后第一条视频号发布照样成功"；
    #   * 线程结束后这把锁就被一个**已经不存在的线程**占着，同账号的下一个任务（新线程）
    #     永远拿到 False → 一直打 🕐「正被其它任务占用」、真正的发布卡满 LOCK_WAIT_TIMEOUT
    #     才失败，只有重启后端能回收（2026-09-29 的真实事故，一天里连撞多次）。
    # 所以：**一次 acquire 决定"拿到 / 降级 / 超时"，一次 release 还回去**，别拆成两次。
    # 回归测试：tests/test_tencent_session_busy_fallback.py。
    lock = account_lock(target)
    held = (
        lock.acquire(blocking=False)
        if fallback_when_busy
        else lock.acquire(timeout=LOCK_WAIT_TIMEOUT)
    )

    if not held and fallback_when_busy:
        tencent_logger.info(
            _msg("🕐", f"{target.name} 正被其它任务占用（多半在发布），本次校验用只读方式，不等它")
        )
        launch_kwargs, context_kwargs = build_ephemeral_kwargs(headless)
        browser = await playwright.chromium.launch(**launch_kwargs)
        try:
            kwargs = {"storage_state": str(account_path)} if account_path and account_path.exists() else {}
            kwargs.update(context_kwargs)
            context = await browser.new_context(**kwargs)
            context = await set_init_script(context)
            yield TencentSession(
                context=context, profile_dir=target, account_file=account_path, ephemeral=True
            )
        finally:
            try:
                await browser.close()
            except Exception:
                pass
        return

    # 等 profile 锁也要**有上限**：线程杀不掉，一旦有个任务卡在收尾环节，它会永久占着
    # 这把锁，之后这个账号的每次发布都干等在这里，既不报错也不超时。
    # 超时就抛 BrowserLockTimeout，让上层带着明确原因失败（用户据此重启后端回收）。
    if not held:
        tencent_logger.error(
            _msg("⏳", f"{target.name} 的浏览器会话被占用超过 {LOCK_WAIT_TIMEOUT / 60:.0f} 分钟，放弃等待")
        )
        raise BrowserLockTimeout(
            f"等 {target.name} 的浏览器 profile 空出来超过 {LOCK_WAIT_TIMEOUT / 60:.0f} 分钟，已放弃。"
            f"通常意味着上一个任务卡死了、它占着 profile 不放——重启后端可以立即回收。"
        )
    try:
        context = await playwright.chromium.launch_persistent_context(
            str(target), **build_launch_kwargs(headless)
        )
        seeded = 0
        try:
            # 防检测脚本三处统一：以前只有校验和登录加，发布那条路没加
            context = await set_init_script(context)
            if seed and account_path is not None:
                seeded = await seed_cookies(context, account_path)
            yield TencentSession(
                context=context, profile_dir=target, account_file=account_path, seeded=seeded
            )
        finally:
            # 收尾必须**有界**：这两步在 `with lock:` 里跑，任何一步不返回，
            # profile 锁就永远不释放，之后这个账号的发布全都会卡在等锁上。
            # 所以都用 wait_for 包一层，超时就放弃并大声记一笔——留个没关干净的
            # 浏览器，比让一个账号永久卡死轻得多。见 CLEANUP_TIMEOUT 的说明。
            if persist and account_path is not None:
                try:
                    await asyncio.wait_for(
                        save_storage_state(context, account_path), timeout=CLEANUP_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    tencent_logger.warning(
                        _msg("⏱️", f"回写 cookie 超时（{CLEANUP_TIMEOUT:.0f}s），已放弃——继续关浏览器，锁必须释放")
                    )
                except Exception as exc:
                    tencent_logger.warning(_msg("😵", f"回写 cookie 失败（不影响本次发布结果）: {exc}"))
            try:
                await asyncio.wait_for(context.close(), timeout=CLEANUP_TIMEOUT)
            except asyncio.TimeoutError:
                tencent_logger.warning(
                    _msg(
                        "⏱️",
                        f"关闭浏览器超时（{CLEANUP_TIMEOUT:.0f}s），已放弃等待。"
                        f"可能留下一个没关干净的 Chrome，但 profile 锁会正常释放",
                    )
                )
            except Exception:
                pass
    finally:
        # 锁一定释放：上面收尾那两步已经用 wait_for 兜过底，这里是最后一道保险
        lock.release()


@dataclass
class ProfilePlacement:
    """登录后 profile 的落点。`created=True` 表示这个目录是这次登录新建的。"""

    path: Path
    created: bool


def finalize_login_profile(account_file: str | Path, staging_dir: str | Path) -> ProfilePlacement:
    """把登录用的临时 profile 归位成 `uin_<wxuin>`（**必须在浏览器关闭之后调用**）。

    如果这个微信号以前登录过（目录已存在），**保留老目录、丢掉这次的临时目录**：
    老目录里是更完整的设备痕迹（其它域的 cookie、IndexedDB、localStorage），而这次新拿到的
    sessionid 会在下次启动时由 `seed_cookies()` 注入进去。反过来做（拿新目录覆盖老目录）
    等于每次重新扫码都换一台设备，正是要避免的事。
    """
    staging = Path(staging_dir)
    uin = read_uin(account_file)
    if not uin:
        tencent_logger.warning(
            _msg("⚠️", f"没能从账号文件里读到微信号，登录态先放在临时 profile: {staging.name}")
        )
        return ProfilePlacement(path=staging, created=True)

    final = profile_root() / f"uin_{uin}"
    if final.exists() and final != staging:
        discard_profile(staging)
        tencent_logger.info(_msg("♻️", f"这个微信号已有浏览器 profile，复用它: {final.name}"))
        return ProfilePlacement(path=final, created=False)

    try:
        staging.rename(final)
        tencent_logger.info(_msg("🗂️", f"浏览器 profile 已归位: {final.name}"))
        return ProfilePlacement(path=final, created=True)
    except Exception as exc:
        tencent_logger.warning(_msg("⚠️", f"profile 归位失败（继续用临时目录）: {exc}"))
        return ProfilePlacement(path=staging, created=True)


__all__ = [
    "CHANNELS_DOMAIN",
    "CLEANUP_TIMEOUT",
    "SESSION_COOKIE",
    "TencentSession",
    "ProfilePlacement",
    "build_launch_kwargs",
    "cleanup_stale_lock",
    "discard_profile",
    "finalize_login_profile",
    "new_staging_dir",
    "profile_dir_for",
    "profile_root",
    "read_uin",
    "save_storage_state",
    "seed_cookies",
    "tencent_session",
]
