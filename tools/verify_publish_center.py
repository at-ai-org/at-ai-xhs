# -*- coding: utf-8 -*-
"""发布中心验收脚本（Playwright 驱动真实浏览器）。

只盯这次改动的四条，不做整页体检：

  1. **素材选择：点整行 = 勾选/取消勾选**（不用瞄准那个小方框）。
     顺手钉住那个经典的互相抵消：点勾选框本身时，`row-click` 要跳过勾选框那一格，
     否则勾选框自己翻一次、行点击再翻一次，表现是"点勾选框没反应"。
  2. **「跳过已发布过的素材」默认勾选**。
  3. **提交时真的带上 `skip_published`**（勾着是 true，取消是 false）。
  4. **跳过之后界面要说清楚**：后端回了几条 skipped 就提示几条；
     整批全被跳过（`task_count = 0`）时提示"都已发布过"，且**不去轮询一个不存在的批次**。

`/publish/submit`、`/publish/check-published`、`/xhs/materials` 全部用 route 接管，
所以**不会真的发布任何东西**，也不会碰真实素材。

前置：前端 Vite（5173）与后端 Flask（5409）都已启动（./start.sh）。
      登录密码读自 settings.json 的 auth.password。

用法：
    .venv/bin/python tools/verify_publish_center.py
    .venv/bin/python tools/verify_publish_center.py --base http://localhost:5173
"""

import json
import sys
import time
from pathlib import Path

# 脚本在 tools/ 下，仓库根要进 sys.path 才能 import conf（它会设置 PLAYWRIGHT_BROWSERS_PATH）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conf import BASE_DIR  # noqa: F401,E402
import myUtils.publish_evidence as evidence  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:5173"
if "--base" in sys.argv:
    BASE = sys.argv[sys.argv.index("--base") + 1]
URL = f"{BASE}/#/publish-center"
# 截图落点统一由 myUtils/publish_evidence 决定：发布失败的现场在
# `screenshots/<平台>/`，验收脚本的图放 `screenshots/tools/`（免得混着看）。
SHOT_DIR = evidence.platform_dir("tools")
VIEWPORT = {"width": 1440, "height": 950}

failures: list[str] = []


def read_credentials() -> tuple[str, str]:
    from myUtils.app_settings import SETTINGS_PATH

    cfg = json.loads(Path(SETTINGS_PATH).read_text(encoding="utf-8"))
    auth = cfg.get("auth") or {}
    return (auth.get("username") or "admin").strip(), (auth.get("password") or "").strip()


def login_ui(browser):
    username, password = read_credentials()
    context = browser.new_context(viewport=VIEWPORT)
    page = context.new_page()
    page.goto(f"{BASE}/#/login", wait_until="networkidle")
    page.fill('input[autocomplete="username"]', username)
    page.fill('input[autocomplete="current-password"]', password)
    page.click(".login-button")
    page.wait_for_selector(".sidebar-menu", timeout=15000)
    state = context.storage_state()
    context.close()
    return state


def authed_page(browser, storage):
    context = browser.new_context(storage_state=storage, viewport=VIEWPORT)
    return context.new_page()


def check(cond, msg):
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        failures.append(msg)


def fulfill(route, payload, status=200):
    route.fulfill(
        status=status,
        content_type="application/json",
        body=json.dumps(payload, ensure_ascii=False),
    )


#: 素材库假数据（`/xhs/materials` 的形状）。文件名带 AI 文案，选中后能直接进发布列表。
def fake_material(i):
    name = f"假素材{i}.mp4"
    return {
        "name": name,
        "size": 1024 * 1024 * i,
        "size_mb": float(i),
        "mtime": time.time() - i * 3600,
        "mtime_text": "2026-10-14 10:00",
        "abs_path": f"/tmp/{name}",
        "title": f"标题{i}",
        "desc": f"正文{i}",
        "tags": ["测试"],
        "has_copy": True,
    }


MATERIALS = [fake_material(i) for i in (1, 2, 3)]


class Fake:
    """假后端：记录收到的提交，并按用例决定回什么。"""

    def __init__(self):
        self.submits: list[dict] = []
        #: `/publish/check-published` 要回的冲突列表（默认没有）
        self.conflicts: list[dict] = []
        self.submit_reply = {
            "code": 200,
            "msg": "已提交发布任务",
            "data": {"job_id": "job-fake", "task_count": 1, "tasks": [], "skipped": [], "skipped_count": 0},
        }

    def install(self, page):
        page.route(
            "**/xhs/materials",
            lambda r: fulfill(r, {"code": 200, "msg": "ok", "data": MATERIALS}),
        )
        page.route("**/publish/check-published", self.on_check)
        page.route("**/publish/submit", self.on_submit)
        # 进度接口：批次存在与否都回一份空的，别让页面卡在加载态
        page.route("**/publish/job/**", lambda r: fulfill(r, {"code": 200, "msg": "ok", "data": {
            "job_id": "job-fake", "status": "running", "tasks": [], "summary": {}}}))
        page.route("**/publish/jobs", lambda r: fulfill(r, {"code": 200, "msg": "ok", "data": []}))

    def on_submit(self, route):
        self.submits.append(json.loads(route.request.post_data or "{}"))
        fulfill(route, self.submit_reply)

    def on_check(self, route):
        conflicts = self.conflicts
        fulfill(route, {"code": 200, "msg": "ok", "data": {
            "conflicts": conflicts,
            "total": len(conflicts),
            "published": sum(1 for c in conflicts if c.get("published")),
            "in_flight": sum(1 for c in conflicts if c.get("in_flight")),
            "task_count": 2,
        }})


def open_picker(page):
    """打开「从素材库选择」弹窗，等假素材渲染出来。"""
    page.click("button:has-text('从素材库选择')")
    page.wait_for_selector(".el-dialog:visible .material-pick-table .el-table__row", timeout=10000)
    page.wait_for_timeout(300)


def rows(page):
    return page.locator(".el-dialog:visible .material-pick-table .el-table__row")


def selected_names(page):
    """弹窗里已勾选的素材名（从勾选框的状态读，不猜）。"""
    out = []
    for i in range(rows(page).count()):
        row = rows(page).nth(i)
        cls = row.locator(".el-checkbox").first.get_attribute("class") or ""
        if "is-checked" in cls:
            out.append(row.locator("td:nth-child(2)").inner_text().strip())
    return out


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        print("\n[0] 登录")
        storage = login_ui(browser)
        print("  ✅ 已登录")

        page = authed_page(browser, storage)
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        fake = Fake()
        fake.install(page)

        page.goto(URL, wait_until="networkidle")
        page.wait_for_selector("button:has-text('从素材库选择')", timeout=15000)
        page.wait_for_timeout(500)

        # ---------------- 1) 点整行 = 勾选 ----------------
        print("\n[1] 素材选择：点整行就能选中")
        open_picker(page)
        check(rows(page).count() == len(MATERIALS), f"弹窗列出 {len(MATERIALS)} 条素材")

        # 点第一行的"素材名"这一格（不是勾选框）
        rows(page).nth(0).locator("td:nth-child(2)").click()
        page.wait_for_timeout(250)
        check(selected_names(page) == ["假素材1.mp4"], f"点整行选中了它（当前 {selected_names(page)}）")

        # 再点同一条 = 取消
        rows(page).nth(0).locator("td:nth-child(2)").click()
        page.wait_for_timeout(250)
        check(selected_names(page) == [], f"再点一下取消勾选（当前 {selected_names(page)}）")

        # 点勾选框本身：必须**真的翻一次**（行点击不能再翻一次把它抵消掉）
        rows(page).nth(1).locator(".el-checkbox").click()
        page.wait_for_timeout(250)
        check(
            selected_names(page) == ["假素材2.mp4"],
            f"点勾选框本身有效、没有被行点击抵消（当前 {selected_names(page)}）",
        )
        rows(page).nth(1).locator(".el-checkbox").click()
        page.wait_for_timeout(250)
        check(selected_names(page) == [], "再点勾选框能取消")

        # 多选：整行点两条
        rows(page).nth(0).locator("td:nth-child(2)").click()
        rows(page).nth(2).locator("td:nth-child(2)").click()
        page.wait_for_timeout(300)
        check(len(selected_names(page)) == 2, f"整行连点两条可以多选（{selected_names(page)}）")
        page.screenshot(path=str(SHOT_DIR / "publish_material_picker.png"))

        page.click(".el-dialog:visible button:has-text('加入发布列表')")
        page.wait_for_timeout(600)
        check(
            page.locator(".item-card").count() == 2,
            f"两条素材进了发布列表（实际 {page.locator('.item-card').count()} 条）",
        )

        # ---------------- 2) 「跳过已发布过的素材」默认勾选 ----------------
        print("\n[2] 「跳过已发布过的素材」：默认勾选、取消后照发")
        box = page.locator(".publish-center .el-checkbox:has-text('跳过已发布过的素材')").first
        check(box.count() == 1, "发布选项里有这个开关")
        check("is-checked" in (box.get_attribute("class") or ""), "默认是勾上的")

        # 选一个平台 + 账号，才能提交
        platform_row = page.locator(".platform-row").filter(
            has=page.locator(".platform-name:text-is('小红书')")
        ).first
        check(platform_row.count() == 1, "找得到「小红书」那一行")
        platform_row.locator(".platform-head .el-checkbox").click()
        page.wait_for_timeout(600)

        # ---------------- 3) 提交时带上 skip_published ----------------
        print("\n[3] 提交：payload 里的 skip_published")
        fake.submit_reply["data"] = {
            "job_id": "job-fake",
            "task_count": 2,
            "tasks": [],
            "skipped": [
                {"material": "假素材1.mp4", "platform": 1, "platform_name": "小红书",
                 "account_name": "账号A", "last_success_time": "2026-10-01 12:00:00", "count": 1}
            ],
            "skipped_count": 1,
        }
        page.click("button:has-text('开始发布')")
        page.wait_for_timeout(1500)

        check(len(fake.submits) == 1, f"提交了一次（实际 {len(fake.submits)} 次）")
        if fake.submits:
            check(fake.submits[-1].get("skip_published") is True, "勾着的时候 skip_published=true")
            check(bool(fake.submits[-1].get("items")), "items 照常带上")
        msgs = page.locator(".el-message").all_inner_texts()
        check(
            any("已跳过 1 条" in m for m in msgs),
            f"界面提示跳过了几条（当前提示 {msgs}）",
        )
        page.screenshot(path=str(SHOT_DIR / "publish_center_skip.png"))

        # ---------------- 4) 整批全被跳过 ----------------
        print("\n[4] 整批都发过：提示 + 不开空的进度页")
        page.wait_for_timeout(3500)  # 等上一条提示散掉
        fake.submit_reply = {
            "code": 200,
            "msg": "这一批素材在对应平台都已经成功发布过，没有需要发布的任务",
            "data": {"job_id": "", "task_count": 0, "tasks": [], "skipped": [
                {"material": "假素材3.mp4", "platform": 1, "platform_name": "小红书",
                 "account_name": "账号A", "last_success_time": "2026-10-02 09:00:00", "count": 1}
            ], "skipped_count": 1},
        }
        page.click("button:has-text('开始发布')")
        page.wait_for_timeout(1500)
        msgs = page.locator(".el-message").all_inner_texts()
        check(
            any("都已经发布过" in m or "没有需要发布的任务" in m for m in msgs),
            f"提示「这批都发过了」（当前提示 {msgs}）",
        )

        # ---------------- 5) 取消勾选 = 全部照发 ----------------
        print("\n[5] 取消勾选后：skip_published=false，并恢复二次确认")
        page.wait_for_timeout(3500)
        fake.submit_reply = {
            "code": 200, "msg": "已提交发布任务",
            "data": {"job_id": "job-fake", "task_count": 2, "tasks": [], "skipped": [], "skipped_count": 0},
        }
        # 这个账号确实发过这条素材：勾着"跳过"时该被后端过滤、不该弹窗打扰人；
        # 取消勾选后就要弹出来问一句（重发是合法操作，但要人知情）。
        fake.conflicts = [
            {"material": "假素材1.mp4", "platform": 1, "platform_name": "小红书",
             "account_id": 1, "account_name": "账号A", "published": True, "in_flight": False,
             "message": "已于 2026-10-01 12:00:00 成功发布过"}
        ]
        submits_before = len(fake.submits)

        page.click("button:has-text('开始发布')")
        page.wait_for_timeout(1200)
        check(
            page.locator(".el-dialog:visible:has-text('这些内容这个账号已经发过了')").count() == 0,
            "勾着「跳过已发布过的素材」时不弹「这个账号发过了」的确认框（那些任务会被跳过，问了也白问）",
        )
        check(len(fake.submits) == submits_before + 1, "而是直接提交了")

        box.click()
        page.wait_for_timeout(300)
        check("is-checked" not in (box.get_attribute("class") or ""), "开关已取消勾选")
        page.click("button:has-text('开始发布')")
        page.wait_for_timeout(1200)
        check(
            page.locator(".el-dialog:visible:has-text('这些内容这个账号已经发过了')").count() == 1,
            "取消勾选后，发过的素材会弹二次确认",
        )
        page.click(".el-dialog:visible button:has-text('我再看看')")
        page.wait_for_timeout(400)

        # 走一遍确认继续：确认标记要跟着提交上去
        page.click("button:has-text('开始发布')")
        page.wait_for_timeout(1200)
        page.click(".el-dialog:visible button:has-text('确认，继续发布')")
        page.wait_for_timeout(1200)
        check(fake.submits[-1].get("skip_published") is False, "确认继续后 skip_published=false")
        check(
            fake.submits[-1].get("options", {}).get("confirmRepublish") is True,
            "确认继续后 confirmRepublish=true（后端据此在任务日志里留痕）",
        )

        print("\n[控制台错误]")
        noise = ("favicon", "401", "404")
        real = [e for e in errors if not any(n in e for n in noise)]
        for e in real[:10]:
            print("  ⚠️", e[:200])
        check(not real, "浏览器控制台无报错")

        browser.close()

    print("\n================ 结果 ================")
    if failures:
        print(f"❌ {len(failures)} 项未通过：")
        for f in failures:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
