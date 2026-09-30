# -*- coding: utf-8 -*-
"""登录界面 / 全站鉴权验收脚本（Playwright 驱动真实浏览器）。

这个脚本存在的理由：**整个后台都要登录**这件事，光看单测是看不出来的。
`tests/test_web_auth.py` 验的是后端那一层的"挡不挡得住"；这里验的是真实浏览器里
"用户到底能不能进得去、进不去的时候看到什么"——路由守卫、token 存取、401 跳转、
退出登录这些串起来的链路，只有在真浏览器里跑才算数。

检查项：
  1. 未登录直接访问受保护页面（#/material-management）→ 被赶到登录页；
  2. 密码填错 → 留在登录页，并弹出后端的提示（不能"静悄悄地什么也不发生"）；
  3. 密码正确 → 进得去（侧边栏出现），且业务数据真的加载出来了
     （素材列表有行 —— 证明 token 确实挂在了接口请求上）；
  4. 刷新页面 → 仍是登录态（token 存在 localStorage，不是内存里）；
  5. 伪造一个 token → 被后端 401 赶回登录页（守卫拦不住伪造，后端能）；
  6. 点「退出登录」→ 回登录页，且 localStorage 里的 token 已清空；
  7. 不带 token 直接打接口 → 401（确认后端是真的在挡，不只是前端在遮）。

前置：前端与后端都已启动（./start.sh）；密码读自 settings.json 的 auth.password。

用法：
    .venv/bin/python tools/verify_login_page.py
    .venv/bin/python tools/verify_login_page.py --headed          # 想看着它跑
    .venv/bin/python tools/verify_login_page.py --base http://127.0.0.1:5175 --api http://127.0.0.1:5410

注：脚本会故意用错密码试一次，但紧接着就会成功登录一次——成功登录会清掉失败计数
（见 myUtils/web_auth.py 的 clear_failures），所以反复跑不会把自己锁掉。
"""

import argparse
import json
import sys
from pathlib import Path

# 脚本在 tools/ 下，仓库根要进 sys.path 才能 import conf（它会设置 PLAYWRIGHT_BROWSERS_PATH）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conf import BASE_DIR  # noqa: F401,E402  # 导入它会把 PLAYWRIGHT_BROWSERS_PATH 指到项目内的 .browsers/
from playwright.sync_api import sync_playwright  # noqa: E402

SHOT_DIR = Path("screenshots")
SHOT_DIR.mkdir(exist_ok=True)

#: 与 sau_frontend/src/utils/session.js 里的 TOKEN_KEY 保持一致。
#: 这里刻意写死：脚本就是要盯住这个"前后端的约定"，改名字了两边都得改，测试该红。
TOKEN_KEY = "at-ai-xhs-token"

failures: list[str] = []


def check(cond, msg):
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        failures.append(msg)


def read_credentials() -> tuple[str, str]:
    """从 settings.json 读登录账号密码（首次启动后端时自动生成）。"""
    from myUtils.app_settings import SETTINGS_PATH

    if not Path(SETTINGS_PATH).is_file():
        raise SystemExit(f"找不到 {SETTINGS_PATH}：先启动一次后端（./start.sh），它会生成随机密码")
    auth = (json.loads(Path(SETTINGS_PATH).read_text(encoding="utf-8")).get("auth") or {})
    password = (auth.get("password") or "").strip()
    if not password:
        raise SystemExit(f"{SETTINGS_PATH} 里没有 auth.password：启动一次后端会自动补上")
    return (auth.get("username") or "admin").strip(), password


def fill_login(page, username, password):
    page.fill('input[autocomplete="username"]', username)
    page.fill('input[autocomplete="current-password"]', password)
    page.click(".login-button")


def main():
    parser = argparse.ArgumentParser(description="登录界面 / 全站鉴权验收")
    parser.add_argument("--base", default="http://127.0.0.1:5173", help="前端地址")
    parser.add_argument("--api", default="http://127.0.0.1:5409", help="后端地址（用于绕过前端直接打接口）")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    args = parser.parse_args()

    username, password = read_credentials()
    login_url = f"{args.base}/#/login"
    protected_url = f"{args.base}/#/material-management"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1440, "height": 950})
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        # ---------------- 1) 未登录访问受保护页面 ----------------
        print("\n[1] 未登录时直接访问受保护页面")
        page.goto(protected_url, wait_until="networkidle")
        page.wait_for_timeout(600)
        check(page.locator(".login-button").count() == 1, "被路由守卫赶到了登录页（看到登录按钮）")
        check(
            page.locator(".sidebar-menu").count() == 0,
            "没有渲染出侧边栏（没登录就看不到后台布局）",
        )
        page.screenshot(path=str(SHOT_DIR / "login_page.png"))

        # ---------------- 2) 密码填错 ----------------
        print("\n[2] 密码填错")
        fill_login(page, username, "definitely-not-the-password")
        page.wait_for_selector(".el-message", timeout=8000)
        message = page.locator(".el-message").first.inner_text()
        check("密码" in message or "不正确" in message, f"弹出了后端的提示：{message.strip()}")
        page.wait_for_timeout(400)
        check(page.locator(".login-button").count() == 1, "仍然留在登录页")
        check(
            not page.evaluate(f"localStorage.getItem({TOKEN_KEY!r})"),
            "失败的登录没有写进任何 token",
        )
        page.wait_for_timeout(2600)  # 等提示自己消失，别影响下面的截图

        # ---------------- 3) 密码正确 ----------------
        print("\n[3] 密码正确")
        fill_login(page, username, password)
        page.wait_for_selector(".sidebar-menu", timeout=15000)
        check(page.locator(".sidebar-menu").count() == 1, "进入了系统（侧边栏出现）")
        token = page.evaluate(f"localStorage.getItem({TOKEN_KEY!r})")
        check(bool(token), "token 已写入 localStorage")
        check(page.locator(".user-dropdown .username").inner_text().strip() == username, f"顶栏显示当前账号：{username}")

        # 业务数据是真的加载出来了——证明 token 确实挂在了接口请求上
        page.goto(protected_url, wait_until="networkidle")
        page.wait_for_selector(".material-list .el-table__row", timeout=15000)
        rows = page.locator(".material-list .el-table__body-wrapper tbody tr").count()
        check(rows > 0, f"登录后业务接口正常返回（素材列表 {rows} 行）")
        page.screenshot(path=str(SHOT_DIR / "login_after_login.png"), full_page=True)

        # ---------------- 4) 刷新后仍是登录态 ----------------
        print("\n[4] 刷新页面")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(800)
        check(page.locator(".sidebar-menu").count() == 1, "刷新后仍停留在后台，没有被踢回登录页")

        # ---------------- 5) 伪造 token ----------------
        print("\n[5] 把 token 改成一个伪造值再访问")
        page.evaluate(f"localStorage.setItem({TOKEN_KEY!r}, 'forged.token.value')")
        page.goto(protected_url, wait_until="networkidle")
        page.wait_for_timeout(1500)
        check(page.locator(".login-button").count() == 1, "伪造的 token 被后端 401 挡下，赶回了登录页")
        check(
            not page.evaluate(f"localStorage.getItem({TOKEN_KEY!r})"),
            "失效的 token 已被清掉（不会一直拿着一个坏 token 反复 401）",
        )

        # ---------------- 6) 退出登录 ----------------
        print("\n[6] 重新登录后点「退出登录」")
        page.goto(login_url, wait_until="networkidle")
        fill_login(page, username, password)
        page.wait_for_selector(".sidebar-menu", timeout=15000)
        page.hover(".user-dropdown")
        page.wait_for_selector(".el-dropdown-menu", timeout=5000)
        page.click(".el-dropdown-menu >> text=退出登录")
        page.wait_for_selector(".login-button", timeout=8000)
        check(page.locator(".login-button").count() == 1, "退出后回到登录页")
        check(
            not page.evaluate(f"localStorage.getItem({TOKEN_KEY!r})"),
            "localStorage 里的 token 已清空",
        )

        # ---------------- 7) 绕过前端直接打接口 ----------------
        print("\n[7] 不带 token 直接请求后端接口")
        resp = page.request.get(f"{args.api}/getFiles")
        check(resp.status == 401, f"GET /getFiles 返回 401（实际 {resp.status}）")
        body = resp.json()
        check(body.get("code") == 401, f"返回体是规范的 JSON：{body.get('msg')}")

        resp = page.request.get(f"{args.api}/auth/status")
        check(resp.status == 200, "GET /auth/status 允许匿名访问（前端靠它探活）")

        # 页面控制台不该有意外报错（登录页那次 401 是走正常流程的，不算）
        real_errors = [e for e in errors if "401" not in e and "Failed to load resource" not in e]
        check(not real_errors, f"没有意外的控制台报错（{len(real_errors)} 条）")
        for e in real_errors[:5]:
            print(f"      · {e}")

        browser.close()

    print("\n" + "=" * 60)
    if failures:
        print(f"❌ 有 {len(failures)} 项没通过：")
        for f in failures:
            print(f"   · {f}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
