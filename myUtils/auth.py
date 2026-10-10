import asyncio
import configparser
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Mapping, Sequence

from playwright.async_api import async_playwright
from xhs import XhsClient

from conf import BASE_DIR, LOCAL_CHROME_HEADLESS, PDD_HEADLESS
from myUtils.browser_lock import MAX_CONCURRENT_BROWSERS, BrowserLockTimeout, browser_session
from utils.base_social_media import set_init_script
from utils.log import kuaishou_logger, douyin_logger
from pathlib import Path
from uploader.xhs_uploader.main import sign_local


async def _persist_state(context, account_file, save_state: bool) -> None:
    """校验通过后，把浏览器里的 cookie / localStorage 写回账号文件。

    为什么续期必须回写：这些平台的登录态是**服务端会话**，一次访问之后服务端常常会
    轮换票据（新 sessionid 之类）。不回写的话，新票随浏览器一起销毁，本地文件里那条
    旧票已经被服务端作废——**校验一次反而更快掉线**，这正是视频号那条链路踩过的坑
    （见 uploader/tencent_uploader/session.py 与 docs/视频号会话保活.md）。

    只在**校验通过**时调用：没过校验说明页面已经跳到登录页，那时的 cookie 里是失效票据，
    写回去只会把还能用的旧文件覆盖成一份坏的。
    """
    if not save_state:
        return
    try:
        await context.storage_state(path=str(account_file))
    except Exception as exc:  # 回写失败不该让"校验通过"这个结论反转
        print(f"⚠️ 回写 cookie 失败（{account_file}）: {exc}")


async def cookie_auth_douyin(account_file, save_state: bool = False):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=LOCAL_CHROME_HEADLESS)
        context = await browser.new_context(storage_state=account_file)
        context = await set_init_script(context)
        # 创建一个新的页面
        page = await context.new_page()
        # 访问指定的 URL
        await page.goto("https://creator.douyin.com/creator-micro/content/upload")
        try:
            await page.wait_for_url("https://creator.douyin.com/creator-micro/content/upload", timeout=5000)
            # 2024.06.17 抖音创作者中心改版
            # 判断
            # 等待“扫码登录”元素出现，超时 5 秒（如果 5 秒没出现，说明 cookie 有效）
            try:
                await page.get_by_text("扫码登录").wait_for(timeout=5000)
                douyin_logger.error("[+] cookie 失效，需要扫码登录")
                return False
            except:
                douyin_logger.success("[+]  cookie 有效")
                await _persist_state(context, account_file, save_state)
                return True
        except:
            douyin_logger.error("[+] 等待5秒 cookie 失效")
            await context.close()
            await browser.close()
            return False


async def cookie_auth_tencent(account_file):
    """视频号登录态校验。

    统一走 uploader 那条「持久化 profile + 回写 cookie + 统一启动参数」的路
    （实现见 `uploader/tencent_uploader/session.py`）。以前这里是自己开一个一次性浏览器
    （`new_context(storage_state=…)`，5 秒内出现「微信小店」就当失效）：既不回写 cookie，
    启动参数也和发布时不一样——同一个账号在两种「设备指纹」下被使用，正是要避免的事。

    顺带说明：换成 uploader 的判定后，结论也更可靠了——它等的是「页面被 JS 跳转到
    login.html」或「出现扫码 iframe」（上游 `662633b` 的修法），而不是靠某个 DOM 元素
    5 秒内有没有出现（页面没加载出来时会误报「有效」）。
    """
    from uploader.tencent_uploader.main import cookie_auth as _tencent_cookie_auth

    return await _tencent_cookie_auth(Path(account_file))


async def cookie_auth_ks(account_file, save_state: bool = False):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=LOCAL_CHROME_HEADLESS)
        context = await browser.new_context(storage_state=account_file)
        context = await set_init_script(context)
        # 创建一个新的页面
        page = await context.new_page()
        # 访问指定的 URL
        await page.goto("https://cp.kuaishou.com/article/publish/video")
        try:
            await page.wait_for_selector("div.names div.container div.name:text('机构服务')", timeout=5000)  # 等待5秒

            kuaishou_logger.info("[+] 等待5秒 cookie 失效")
            return False
        except:
            kuaishou_logger.success("[+] cookie 有效")
            await _persist_state(context, account_file, save_state)
            return True


async def cookie_auth_xhs(account_file, save_state: bool = False):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=LOCAL_CHROME_HEADLESS)
        context = await browser.new_context(storage_state=account_file)
        context = await set_init_script(context)
        # 创建一个新的页面
        page = await context.new_page()
        # 访问指定的 URL
        await page.goto("https://creator.xiaohongshu.com/creator-micro/content/upload")
        try:
            await page.wait_for_url("https://creator.xiaohongshu.com/creator-micro/content/upload", timeout=5000)
        except:
            print("[+] 等待5秒 cookie 失效")
            await context.close()
            await browser.close()
            return False
        # 2024.06.17 抖音创作者中心改版
        if await page.get_by_text('手机号登录').count() or await page.get_by_text('扫码登录').count():
            print("[+] 等待5秒 cookie 失效")
            return False
        else:
            print("[+] cookie 有效")
            await _persist_state(context, account_file, save_state)
            return True


async def cookie_auth_pdd(account_file, save_state: bool = False):
    """拼多多视频（达人）登录态校验。

    走 uploader 那条统一的校验实现（`uploader/pdd_uploader/main.py::cookie_auth`）：
    判据是"打开发布页有没有被踹到 /login"，比等某个 DOM 元素可靠——后者在页面慢的
    时候会误报失效，把人骗去重新扫码（抖音那条老路踩过）。

    和快手/抖音一样支持 `save_state`：拼多多的登录态同样是**服务端会话**，
    一次访问之后服务端可能轮换票据，续期时必须把新票写回，否则"校验一次反而更快掉线"。
    """
    from uploader.pdd_uploader.main import cookie_auth as _pdd_cookie_auth

    valid = await _pdd_cookie_auth(Path(account_file))
    if valid:
        # 校验本身是用一次性 context 跑的，回写要靠再开一次。
        # 代价是多次浏览器启动，所以只在明确要求续期时做（session_renew 会传 True）。
        if save_state:
            await _persist_pdd_state(Path(account_file))
    return valid


async def _persist_pdd_state(account_file) -> None:
    """带着账号 cookie 打开发布页并把浏览器里的最新 cookie 回写账号文件。

    为什么不能省：拼多多的会话票据在服务端轮换，本地文件里那条旧票会被作废。
    回写只在**校验通过后**发生（见 `_persist_state` 的说明），失败只记日志。
    """
    async with async_playwright() as playwright:
        # 用拼多多自己的无头开关（PDD_HEADLESS / SAU_PDD_HEADLESS），别和 LOCAL_CHROME_HEADLESS 分叉：
        # 首次接入时用户很可能会把拼多多切成有头来看页面，回写这一步也该跟着有头，
        # 否则"同一账号两种设备指纹"，正是视频号那条链路踩过的坑。
        browser = await playwright.chromium.launch(headless=PDD_HEADLESS)
        try:
            context = await browser.new_context(storage_state=str(account_file))
            context = await set_init_script(context)
            page = await context.new_page()
            await page.goto(
                "https://live.pinduoduo.com/n-creator/video/home",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await page.wait_for_timeout(3000)
            if "/login" not in page.url:
                await _persist_state(context, account_file, True)
            else:
                print("⚠️ 回写拼多多 cookie 时发现已跳到登录页，本次不回写")
        except Exception as exc:  # noqa: BLE001 —— 回写失败不该让"校验通过"这个结论反转
            print(f"⚠️ 回写拼多多 cookie 异常（忽略）: {exc}")
        finally:
            await browser.close()


async def check_cookie(type, file_path, save_state: bool = False):
    """校验登录态；`save_state=True` 时顺带把服务端轮换下来的新票据写回账号文件。

    `save_state` 只有「会话续期」（myUtils/session_renew.py）会传 True：
    那是定期保活，新票据必须留下来；而账号管理页点刷新只是"看一眼"，不该动文件。
    """
    match type:
        # 小红书
        case 1:
            return await cookie_auth_xhs(Path(BASE_DIR / "cookiesFile" / file_path), save_state=save_state)
        # 视频号
        case 2:
            # 视频号的续期由 uploader 那套持久化 profile 负责（校验/发布/失败一律回写），
            # 这里没有额外要做的事，见 uploader/tencent_uploader/session.py。
            return await cookie_auth_tencent(Path(BASE_DIR / "cookiesFile" / file_path))
        # 抖音
        case 3:
            return await cookie_auth_douyin(Path(BASE_DIR / "cookiesFile" / file_path), save_state=save_state)
        # 快手
        case 4:
            return await cookie_auth_ks(Path(BASE_DIR / "cookiesFile" / file_path), save_state=save_state)
        # 拼多多视频
        case 5:
            return await cookie_auth_pdd(Path(BASE_DIR / "cookiesFile" / file_path), save_state=save_state)
        case _:
            return False

# a = asyncio.run(check_cookie(1,"3a6cfdc0-3d51-11f0-8507-44e51723d63c.json"))
# print(a)


# --------------------------------------------------------------------------- #
# 并发校验一批账号（「账号管理」页点刷新时用）
# --------------------------------------------------------------------------- #
#: 账号 cookie 文件所在目录（`user_info.filePath` 就是这里的文件名）。
COOKIE_DIR = Path(BASE_DIR) / "cookiesFile"

#: 校验一个账号时，最多等多久「浏览器名额 / 账号锁」（秒）。
#:
#: 这不是"校验超时"，而是**排队上限**：这个账号正在发布时同账号不能再开第二个浏览器
#: （会互相踢下线），所以要等；但界面上的刷新不能因为这个账号在发东西就无限期转圈。
#: 等不到就当这次没校验——**状态原样不动**，比"猜一个结论"安全。用
#: SAU_VERIFY_WAIT_TIMEOUT 覆盖。后台的定时续期不用这个值：它是 wait_timeout=0，
#: 碰上占用直接让路（见 myUtils/session_renew.py）。
#:
#: 默认 3 分钟，是按**排队**算的："账号管理"页是一个账号一条请求，账号比浏览器名额多时，
#: 多出来的那些要等前面跑完。实测一个账号 25~30 秒，4 个名额就是每 ~30 秒放行 4 个，
#: 180 秒足够排到 20 多个账号——不会出现"账号多了就有一半报没校验"。再往上就得调大它，
#: 或者调大 SAU_MAX_CONCURRENT_BROWSERS。
VERIFY_WAIT_TIMEOUT = float(os.environ.get("SAU_VERIFY_WAIT_TIMEOUT") or 180)

#: 一条校验结论的四种取值。
#:
#: 和 `session_renew` 里那套字面量刻意保持一致（ok / invalid / busy / error），
#: 但**不共用常量**：session_renew 反过来 import 本模块，共用会绕成循环 import。
#: 两边读起来是一回事就够了——那边是"续期"的结论，这边是"校验"的结论。
STATUS_OK = "ok"
STATUS_INVALID = "invalid"
STATUS_BUSY = "busy"
STATUS_ERROR = "error"


def _verify_one(
    account: Mapping[str, Any], *, save_state: bool, wait_timeout: float | None
) -> dict:
    """校验一个账号，返回结论行（**不抛异常**，任何意外都变成一条 error 结论）。

    结论里的 `ok` 有三种值：True（有效）、False（失效）、None（压根没校验成——
    账号被占用、cookie 文件丢了、或者校验过程出错）。把"没校验"和"校验失败"分开，
    是为了让调用方**别去改状态**：fail 才能写库，None 只能原样不动。
    """
    row = {
        "id": account.get("id"),
        "type": int(account.get("type") or 0),
        "file_path": account.get("file_path") or "",
        "name": account.get("name") or "",
    }

    if not row["file_path"]:
        return {**row, "ok": None, "status": STATUS_ERROR, "message": "账号没有 cookie 文件，请重新扫码授权"}
    account_path = COOKIE_DIR / row["file_path"]
    if not account_path.is_file():
        return {
            **row,
            "ok": None,
            "status": STATUS_ERROR,
            "message": f"cookie 文件不存在（{row['file_path']}），请重新扫码授权",
        }

    try:
        # 账号锁 + 整机浏览器名额都在 browser_session 里，和发布中心用的是**同一把**
        # （key 都是 cookiesFile 下的路径）：同一个账号正在发布时，这里不会另开一个浏览器
        # 去踩它。等待有上限，超了就当这次没校验。
        with browser_session(account_path, wait_timeout=wait_timeout):
            ok = asyncio.run(check_cookie(row["type"], row["file_path"], save_state=save_state))
    except BrowserLockTimeout:
        return {**row, "ok": None, "status": STATUS_BUSY, "message": "该账号正在发布/续期中，这次先没校验"}
    except Exception as exc:  # noqa: BLE001 —— 一个账号炸了不该让别的账号也跟着没结论
        return {**row, "ok": None, "status": STATUS_ERROR, "message": f"校验出错: {exc}"}

    if ok:
        return {**row, "ok": True, "status": STATUS_OK, "message": "登录态有效"}
    return {**row, "ok": False, "status": STATUS_INVALID, "message": "登录态已失效，需要重新扫码授权"}


def check_accounts(
    accounts: Sequence[Mapping[str, Any]],
    *,
    save_state: bool = False,
    wait_timeout: float | None = VERIFY_WAIT_TIMEOUT,
    max_workers: int | None = None,
) -> list[dict]:
    """并发校验多个账号的登录态：**一个账号一个线程、一个浏览器**。

    改造的原因（原来的写法在 `/getValidAccounts` 里是一个 for 循环逐个 `await
    check_cookie`）：每个账号都要开一次浏览器、等几个 5 秒级的超时，实测一个账号
    十几秒到几十秒。串行的话 N 个账号就是 **N 份时间加起来**——账号一多，界面上
    就是"所有账号都显示验证中"、等好几分钟，而且第一个账号慢会把后面的全堵住。

    并发之后总耗时约等于**最慢的那个账号**，账号之间互不影响：一个平台挂了/超时了，
    别的账号照样各自出结论。

    真实并发上限不在这里，而是 `browser_lock` 那把"整机同时最多几个浏览器"的闸门
    （默认 4）——所以线程数也只开到那个数（`MAX_CONCURRENT_BROWSERS`）：多开的线程
    只是在闸门前干等，而且一样要受 `wait_timeout` 约束（30 个账号开 30 个线程的话，
    排在最后的那些会白白耗到超时、报成"没校验"）。账号比名额多的时候，
    排队发生在**线程池的队列**里——那里等待是免费的，不消耗 `wait_timeout`。

    入参是 `[{"id", "type", "file_path", "name"}, ...]`（type 就是 `user_info.type`）。
    返回**与入参同序**的结论行列表。调用方负责按结论写库——本函数不碰数据库。
    """
    items = [dict(account) for account in accounts]
    if not items:
        return []

    if len(items) == 1:
        # 一个账号就别为它开线程池了（"账号管理"页是一个账号一条请求，
        # 这一路才是线上真正跑的那条）。
        return [_verify_one(items[0], save_state=save_state, wait_timeout=wait_timeout)]

    workers = max_workers or min(len(items), MAX_CONCURRENT_BROWSERS)
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="verify-account") as pool:
        # map 保序：第 i 条结论一定对应第 i 个账号，调用方可以直接 zip。
        return list(
            pool.map(
                lambda account: _verify_one(
                    account, save_state=save_state, wait_timeout=wait_timeout
                ),
                items,
            )
        )


__all__ = [
    "COOKIE_DIR",
    "STATUS_BUSY",
    "STATUS_ERROR",
    "STATUS_INVALID",
    "STATUS_OK",
    "VERIFY_WAIT_TIMEOUT",
    "check_accounts",
    "check_cookie",
]
