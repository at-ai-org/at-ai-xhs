import asyncio
import base64
import sqlite3
from urllib.parse import urljoin

from playwright.async_api import async_playwright

from myUtils.auth import check_cookie
from utils.base_social_media import set_init_script
import uuid
from pathlib import Path
from conf import BASE_DIR, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH


def _verify_qr_decodable(data_url: str, label: str = "") -> bool:
    """尽最大努力把二维码解一次，只用于**报警**，不阻断流程。

    加这个是因为踩过一次很隐蔽的坑：选择器挑错了图（把品牌横幅当成二维码发出去），
    前端照常显示、用户照常扫、后端照常等——全程没有任何报错，
    但那个码根本不是登录码，最后表现为"扫了却一直请求中"。
    解一次码只要几十毫秒，能在源头把这类问题喊出来。
    """
    try:
        import base64 as _b64
        import io as _io

        import cv2
        import numpy as _np
        from PIL import Image as _Image

        raw = _b64.b64decode(data_url.split(",", 1)[1])
        img = _Image.open(_io.BytesIO(raw)).convert("RGB")
        arr = cv2.cvtColor(_np.array(img), cv2.COLOR_RGB2BGR)
        text, _, _ = cv2.QRCodeDetector().detectAndDecode(arr)
        if text:
            print(f"🔍 [{label}] 二维码内容自检通过: {text[:80]}")
            return True
        print(f"⚠️ [{label}] 取到的图片解不出二维码内容——很可能选错了元素，扫码会无效")
    except Exception as exc:
        print(f"⚠️ [{label}] 二维码自检跳过（{exc}）")
    return False



async def _find_qr_img(page, timeout=70, interval=2.0):
    """找二维码 <img>，返回 src（data URL）。

    判据：**按图片的原始尺寸判断正方形**（naturalWidth/naturalHeight），
    多个候选取 data URL 最长的那个。

    为什么这么绕——踩过两个坑：
      ✗ 用 `img[src*='qr']` 匹配：作用在 base64 上，能否命中纯靠巧合
        （2900 字符的 base64 里出现 "qr" 的概率约一半），换个二维码就失效；
      ✗ 用"最长的 data URL"：抖音页面上「抖音创作者中心」品牌横幅有 7658 字符，
        比真二维码（2906 字符）还长，结果发出去的是横幅，用户根本扫不出登录。

    正确答案是**正方形**这个特征：二维码必然正方形，横幅/图标不是。
    而且必须用 naturalWidth 而不是 getBoundingClientRect——
    二维码的 <img> 很早就进了 DOM 但布局尺寸是 0（抖音要等约 36 秒才渲染），
    按渲染尺寸判断会整个漏掉。
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            src = await page.evaluate(
                """() => {
                    let best = null, bestLen = 0;
                    document.querySelectorAll('img').forEach(el => {
                        const s = el.getAttribute('src') || '';
                        if (!s.startsWith('data:image')) return;
                        const w = el.naturalWidth, h = el.naturalHeight;
                        if (!w || !h) return;              // 还没解码出来
                        if (w < 100) return;               // 太小，不是二维码
                        if (Math.abs(w - h) > 4) return;   // 不是正方形
                        if (s.length > bestLen) { bestLen = s.length; best = s; }
                    });
                    return best;
                }"""
            )
            if src:
                return src
        except Exception:
            pass
        await asyncio.sleep(interval)
    return None


async def _capture_qr_by_screenshot(page):
    """有些平台的二维码不是 <img>，而是内联 SVG（实测抖音就是：180x180 的 inline svg），
    页面上既没有 img 也没有 canvas，任何基于 src 的选择器都拿不到。

    这种情况直接把二维码区域截图，转成 data URL 交给前端。
    """
    handle = await page.evaluate_handle(
        """() => {
            const wrap = document.querySelector('[class*="login-card-double-content-wrap"]')
                      || document.querySelector('[class*="login-card"]')
                      || document.querySelector('[class*="login"]')
                      || document.body;
            if (!wrap) return null;
            // 找卡片里最接近正方形、边长 150-420、且内含 svg/canvas/img 的元素
            let best = null, bestW = 0;
            wrap.querySelectorAll('*').forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width < 150 || r.width > 420) return;
                if (Math.abs(r.width - r.height) > 14) return;
                if (!el.querySelector('svg, canvas, img')) return;
                if (r.width > bestW) { bestW = r.width; best = el; }
            });
            return best;
        }"""
    )
    element = handle.as_element()
    if element is None:
        return None
    try:
        png = await element.screenshot(type="png")
    except Exception as exc:
        print(f"⚠️ 二维码区域截图失败: {exc}")
        return None
    if not png:
        return None
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


# 登录用的浏览器是否无头。
#
# 默认 True（无头）。曾经改成有头想绕过二维码过期问题，但真正卡住的是
# **抖音扫码后还会弹短信二次验证**——无头模式用户看不到那个页面，自然也填不了验证码，
# 登录永远完不成。既然如此，有头也不能自动完成，不如保持无头清爽，
# 超时后引导用户"自己在浏览器里登一次"（见 _manual_login_hint）。
LOGIN_HEADLESS = True

# 各平台的网页登录地址。超时提示里要给出这个地址，让用户能自己去登。
LOGIN_URLS = {
    1: "https://creator.xiaohongshu.com/login",
    2: "https://channels.weixin.qq.com/login.html",
    3: "https://creator.douyin.com/",
    4: "https://cp.kuaishou.com/",
}


def _manual_login_hint(platform: int, seconds: int, saw_sms: bool = False) -> str:
    """登录没完成时给用户的提示，必须带上网页登录地址。"""
    url = LOGIN_URLS.get(platform, "")
    reason = "扫码后还需要短信二次验证，无头模式无法完成验证" if saw_sms else "等待扫码登录超时"
    return (
        f"{reason}（{seconds} 秒内未检测到登录成功）。"
        f"建议先在自己的浏览器里打开 {url} 手动完成登录"
        f"（包括短信验证），登录成功后再回来添加账号。"
    )


# 真正代表"已登录"的会话 cookie 名。只有名字命中这些才算数——
# 否则页面加载过程中随手写的统计类 cookie 会被误判成登录成功，
# 结果是存下一份无效 cookie、账号加进去但用不了，比报错更糟。
_SESSION_COOKIE_HINTS = (
    "sessionid", "sessionid_ss", "sid_tt", "uid_tt", "web_session",
    "kuaishou.web.cp.api_st", "kuaishou.web.cp.api_ph", "wxuin", "snsapi",
)


async def _wait_login(context, page, original_url, timeout=240, interval=3.0, label=""):
    """等扫码登录完成，返回 (是否成功, 依据)。

    同时看三个信号，任一满足即算登录成功——只盯 URL 是不够的：

      ① URL 变了：常规跳转，最直接的信号；
      ② 登录卡片/二维码消失了：抖音这类 SPA 可能不跳转（实测登录页 URL 一直停在
         https://creator.douyin.com/ ），但登录卡片会被路由替换掉；
      ③ cookie 里出现了新的会话项：有些平台是静默写 cookie。

    只用一个信号的话，一旦那个信号在当前平台不成立，前端就会**一直停在"请求中"**，
    而且没有任何报错——这正是抖音号卡住的成因。

    另外每 5 次轮询打一条状态日志，方便下次出问题时直接从日志看出卡在哪。
    """
    baseline = set()
    try:
        baseline = {c.get("name") for c in await context.cookies()}
    except Exception:
        pass

    deadline = asyncio.get_event_loop().time() + timeout
    tick = 0
    saw_login_card = False
    while asyncio.get_event_loop().time() < deadline:
        tick += 1
        # ① URL 跳转
        try:
            if page.url != original_url:
                print(f"✅ [{label}] 检测到跳转: {page.url}")
                return True, "url"
        except Exception:
            return True, "closed"

        # ② 登录卡片消失。
        #    必须"先出现过、再消失"才算数：页面刚打开时卡片可能还没渲染出来，
        #    直接判"不存在=已登录"会立刻误判成功，存下一份空 cookie。
        try:
            still_login = await page.evaluate(
                """() => !!document.querySelector(
                       '[class*="login-card"], [class*="qrcode"], [class*="qr-code"], [class*="login-box"]'
                   )"""
            )
            if still_login:
                saw_login_card = True
            elif saw_login_card:
                print(f"✅ [{label}] 登录卡片已消失，判定登录完成")
                return True, "login-card-gone"
        except Exception:
            pass

        # ③ 出现了会话类 cookie（只认名字，不看"有没有新 cookie"）
        try:
            now = {c.get("name") for c in await context.cookies()}
            hit = {n for n in (now - baseline) if n and any(h in n for h in _SESSION_COOKIE_HINTS)}
            if hit:
                print(f"✅ [{label}] 出现会话 cookie: {sorted(hit)[:4]}")
                return True, "cookie"
        except Exception:
            pass

        if tick % 5 == 0:
            try:
                print(f"⏳ [{label}] 等待扫码中… {tick * interval:.0f}s  url={page.url}")
            except Exception:
                pass

        await asyncio.sleep(interval)

    print(f"⏰ [{label}] 等待扫码登录超时（{timeout}s）")
    return False, "timeout"


# 各平台二维码图片的候选选择器。统一用 CSS，不用 get_by_role("img")：
# 二维码 <img> 的 alt 为空，ARIA 角色不是 img，get_by_role 会一直匹配不到直到超时。
_QR_SELECTORS = (
    "img[src*='qrcode']",
    "img[src*='qr_code']",
    "img[alt*='二维码']",
    "img[class*='qr']",
)
# 注意：这里**故意不放** `img[src*='qr']`。它作用在 base64 上，
# 能否命中完全靠巧合（2900 字符里恰好出现 "qr" 的概率约一半），不可靠。
# 兜底候选：只有明确找不到二维码时才会用到，最后一条是裸 img，
# 但它只在"页面上确实只有一张图"这种极端情况下才合理，所以单独列出来慎用。
_QR_SELECTORS_LOOSE = _QR_SELECTORS + ("img",)


async def _grab_img_src(scope, selectors=_QR_SELECTORS, timeout=40):
    """在 scope（Page 或 FrameLocator）里轮询找图片并返回 src，找不到返回 None。

    轮询是必要的：扫码组件经常比页面主体晚渲染，只探一次很容易扑空。
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for sel in selectors:
            try:
                loc = scope.locator(sel).first
                if await loc.count():
                    src = await loc.get_attribute("src", timeout=3000)
                    if src:
                        return src
            except Exception:
                continue
        await asyncio.sleep(1)
    return None


async def _normalize_qr(context, src, base_url=""):
    """把二维码统一成 data:image/...;base64,... 形式。

    各平台的二维码 src 形态不一：
      - 完整的 data URL（老版本）
      - **相对路径**（视频号现在是 `/connect/qrcode/xxxx`，只有 30 来个字符）
      - 绝对 URL

    前端原来靠"字符串长度 > 100 就当成二维码"来猜，视频号这种短相对路径会被直接丢掉，
    表现就是弹窗一直转圈。这里在服务端统一转成 data URL，前端不用再猜。
    """
    src = (src or "").strip()
    if not src:
        return ""
    if src.startswith("data:"):
        return src

    url = urljoin(base_url, src) if base_url else src
    if not url.startswith("http"):
        return src  # 未知形态，原样交给前端，至少不会更糟

    try:
        resp = await context.request.get(url, timeout=20000)
        if resp.ok:
            body = await resp.body()
            ctype = ((resp.headers or {}).get("content-type") or "image/png").split(";")[0].strip()
            if not ctype.startswith("image/"):
                ctype = "image/png"
            return f"data:{ctype};base64,{base64.b64encode(body).decode('ascii')}"
        print(f"⚠️ 下载二维码失败 HTTP {resp.status}，回退为原始地址")
    except Exception as exc:
        print(f"⚠️ 下载二维码异常（回退为原始地址）: {exc}")
    return url

# 统一获取浏览器启动配置（防风控+引入本地浏览器）
def get_browser_options():
    options = {
        'headless': LOCAL_CHROME_HEADLESS,
        'args': [
            '--disable-blink-features=AutomationControlled',  # 核心防爬屏蔽：去掉 window.navigator.webdriver 标签
            '--lang=zh-CN',
            '--disable-infobars',
            '--start-maximized'
        ]
    }
    # 如果用户在 conf.py 里配置了本地 Chrome，就用本地的，这样成功率极高
    if LOCAL_CHROME_PATH:
        options['executable_path'] = LOCAL_CHROME_PATH

    return options

# 抖音登录
async def douyin_cookie_gen(id, status_queue):
    """抖音登录：**直接复用上游主线的 uploader 实现**，不再自己写。

    上游 README 明确写了「Web 端相关代码仍然保留，但已经不是当前主线，
    不保证可直接运行」——本文件其它平台那套就是这类历史代码，抖音尤其明显：
    新版创作者中心改了 DOM，二维码不再是 <img aria-label="二维码">，
    自己猜选择器必然踩坑。

    上游 uploader/douyin_uploader 那条线是"当前主线重构最完整"的一条，有：
      · 正确的二维码选择器（div#animate_qrcode_container img[src^="data:image"]）
      · 二维码失效自动点击刷新
      · 抖音短信/安全二次验证的处理
      · 登录完成的可靠判定（URL 进 creator-micro 且登录标识消失）
    这里只做一个适配层：把它的 qrcode_callback 接到 SSE 队列上。
    """
    from uploader.douyin_uploader.main import douyin_cookie_gen as _upstream_login

    cookies_dir = Path(BASE_DIR / "cookiesFile")
    cookies_dir.mkdir(exist_ok=True)
    uuid_v1 = uuid.uuid1()
    account_file = cookies_dir / f"{uuid_v1}.json"

    def _on_qrcode(payload):
        # 上游给的 payload 同时带本地路径和 data URL，Web 界面要 data URL
        src = (payload or {}).get("image_data_url") or ""
        if src:
            status_queue.put(src)
            print("✅ 抖音二维码已推送到前端")

    try:
        result = await _upstream_login(
            str(account_file),
            qrcode_callback=_on_qrcode,
            headless=LOGIN_HEADLESS,
            # 上游默认就是 60 次 × 2 秒 = 120 秒，这里保持默认并显式写出来
            max_checks=60,
        )
    except Exception as exc:
        print(f"❌ 抖音登录异常: {exc}")
        result = {"success": False, "message": str(exc)}

    if not result.get("success"):
        print(f"❌ 抖音登录失败: {result.get('message')}")
        # 失败的半成品 cookie 文件不要留着，否则会在账号管理里显示成一个坏账号
        try:
            account_file.unlink(missing_ok=True)
        except Exception:
            pass
        # 关键：把"怎么办"连同登录地址一起推给前端，别只丢一个失败状态让人干瞪眼
        hint = _manual_login_hint(3, 120)
        print(f"💡 {hint}")
        status_queue.put(f"msg:{hint}")
        status_queue.put("500")
        return None

    print(f"✅ 抖音登录成功: {result.get('message')} → {account_file.name}")
    with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO user_info (type, filePath, userName, status) VALUES (?, ?, ?, ?)",
            (3, account_file.name, id, 1),
        )
        conn.commit()
        print("✅ 用户状态已记录")
    status_queue.put("200")
    return None


# 视频号登录
async def get_tencent_cookie(id,status_queue):

    async with async_playwright() as playwright:
        options = {
            'args': [
                '--lang en-GB'
            ],
            'headless': LOCAL_CHROME_HEADLESS,  # Set headless option here
        }
        # Make sure to run headed.
        browser = await playwright.chromium.launch(**options)
        # Setup context however you like.
        context = await browser.new_context()  # Pass any options
        # Pause the page, and start recording manually.
        context = await set_init_script(context)
        page = await context.new_page()
        await page.goto("https://channels.weixin.qq.com")
        original_url = page.url


        # 二维码在 open.weixin.qq.com 的 iframe 里。
        # 用 CSS 选择器定位，不能用 get_by_role("img")——实测匹配数为 0（alt 为空）。
        iframe_locator = page.frame_locator("iframe").first
        src = await _grab_img_src(iframe_locator, timeout=60)
        if not src:
            raise RuntimeError("60 秒内没有拿到视频号二维码（页面可能改版或未加载出来）")
        _frame = next((f for f in page.frames if "open.weixin.qq.com" in f.url), None)
        src = await _normalize_qr(context, src, _frame.url if _frame else page.url)
        _verify_qr_decodable(src, id)
        print("✅ 二维码已就绪:", (src or "")[:60])
        status_queue.put(src)

        if not (await _wait_login(context, page, original_url, timeout=120, label=id))[0]:
            hint = _manual_login_hint(2, 120)
            print(f"💡 {hint}")
            status_queue.put(f"msg:{hint}")
            status_queue.put("500")
            await page.close()
            await context.close()
            await browser.close()
            return None
        uuid_v1 = uuid.uuid1()
        print(f"UUID v1: {uuid_v1}")
        # 确保cookiesFile目录存在
        cookies_dir = Path(BASE_DIR / "cookiesFile")
        cookies_dir.mkdir(exist_ok=True)
        await context.storage_state(path=cookies_dir / f"{uuid_v1}.json")
        result = await check_cookie(2,f"{uuid_v1}.json")
        if not result:
            status_queue.put("500")
            await page.close()
            await context.close()
            await browser.close()
            return None
        await page.close()
        await context.close()
        await browser.close()

        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                                INSERT INTO user_info (type, filePath, userName, status)
                                VALUES (?, ?, ?, ?)
                                ''', (2, f"{uuid_v1}.json", id, 1))
            conn.commit()
            print("✅ 用户状态已记录")
        status_queue.put("200")

# 快手登录
async def get_ks_cookie(id,status_queue):
    async with async_playwright() as playwright:
        options = {
            'args': [
                '--lang en-GB'
            ],
            'headless': LOCAL_CHROME_HEADLESS,  # Set headless option here
        }
        # Make sure to run headed.
        browser = await playwright.chromium.launch(**options)
        # Setup context however you like.
        context = await browser.new_context()  # Pass any options
        context = await set_init_script(context)
        # Pause the page, and start recording manually.
        page = await context.new_page()
        await page.goto("https://cp.kuaishou.com")

        # 定位并点击“立即登录”按钮（类型为 link）
        await page.get_by_role("link", name="立即登录").click()
        await page.get_by_text("扫码登录").click()
        try:
            img_locator = page.get_by_role("img", name="qrcode")
            src = await img_locator.get_attribute("src", timeout=8000)
        except Exception:
            src = await _grab_img_src(page, timeout=40)
        if not src:
            raise RuntimeError("没有拿到快手二维码")
        original_url = page.url
        src = await _normalize_qr(context, src, page.url)
        _verify_qr_decodable(src, id)
        print("✅ 二维码已就绪:", (src or "")[:60])
        status_queue.put(src)

        if not (await _wait_login(context, page, original_url, timeout=120, label=id))[0]:
            hint = _manual_login_hint(4, 120)
            print(f"💡 {hint}")
            status_queue.put(f"msg:{hint}")
            status_queue.put("500")
            await page.close()
            await context.close()
            await browser.close()
            return None
        uuid_v1 = uuid.uuid1()
        print(f"UUID v1: {uuid_v1}")
        # 确保cookiesFile目录存在
        cookies_dir = Path(BASE_DIR / "cookiesFile")
        cookies_dir.mkdir(exist_ok=True)
        await context.storage_state(path=cookies_dir / f"{uuid_v1}.json")
        result = await check_cookie(4, f"{uuid_v1}.json")
        if not result:
            status_queue.put("500")
            await page.close()
            await context.close()
            await browser.close()
            return None
        await page.close()
        await context.close()
        await browser.close()

        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                                        INSERT INTO user_info (type, filePath, userName, status)
                                        VALUES (?, ?, ?, ?)
                                        ''', (4, f"{uuid_v1}.json", id, 1))
            conn.commit()
            print("✅ 用户状态已记录")
        status_queue.put("200")

# 小红书登录
async def xiaohongshu_cookie_gen(id,status_queue):
    async with async_playwright() as playwright:
        options = {
            'args': [
                '--lang en-GB'
            ],
            'headless': LOCAL_CHROME_HEADLESS,  # Set headless option here
        }
        # Make sure to run headed.
        browser = await playwright.chromium.launch(**options)
        # Setup context however you like.
        context = await browser.new_context()  # Pass any options
        context = await set_init_script(context)
        # Pause the page, and start recording manually.
        page = await context.new_page()
        await page.goto("https://creator.xiaohongshu.com/")
        await page.locator('img.css-wemwzq').click()

        try:
            img_locator = page.get_by_role("img").nth(2)
            src = await img_locator.get_attribute("src", timeout=8000)
        except Exception:
            src = await _grab_img_src(page, timeout=40)
        if not src:
            raise RuntimeError("没有拿到小红书二维码")
        original_url = page.url
        src = await _normalize_qr(context, src, page.url)
        _verify_qr_decodable(src, id)
        print("✅ 二维码已就绪:", (src or "")[:60])
        status_queue.put(src)

        if not (await _wait_login(context, page, original_url, timeout=120, label=id))[0]:
            hint = _manual_login_hint(1, 120)
            print(f"💡 {hint}")
            status_queue.put(f"msg:{hint}")
            status_queue.put("500")
            await page.close()
            await context.close()
            await browser.close()
            return None
        uuid_v1 = uuid.uuid1()
        print(f"UUID v1: {uuid_v1}")
        # 确保cookiesFile目录存在
        cookies_dir = Path(BASE_DIR / "cookiesFile")
        cookies_dir.mkdir(exist_ok=True)
        await context.storage_state(path=cookies_dir / f"{uuid_v1}.json")
        result = await check_cookie(1, f"{uuid_v1}.json")
        if not result:
            status_queue.put("500")
            await page.close()
            await context.close()
            await browser.close()
            return None
        await page.close()
        await context.close()
        await browser.close()

        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                           INSERT INTO user_info (type, filePath, userName, status)
                           VALUES (?, ?, ?, ?)
                           ''', (1, f"{uuid_v1}.json", id, 1))
            conn.commit()
            print("✅ 用户状态已记录")
        status_queue.put("200")

# a = asyncio.run(xiaohongshu_cookie_gen(4,None))
# print(a)
