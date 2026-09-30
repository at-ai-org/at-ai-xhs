# -*- coding: utf-8 -*-
"""素材管理页验收脚本（Playwright 驱动真实浏览器）。

检查项：
  1. 真实数据：按上传时间倒序、一页最多 20 条、AI 文案列「已有/无」、
     「背景音乐」列（待合成/合成中/已合成，紧跟 AI 文案、UUID 列已去掉）、
     有文案→「预览文案」、无文案→「生成文案」、预览弹窗、搜索重置页码；
  2. 抓包替换成 45 条假素材，验证 20 条/页 的翻页行为；
  3. 上传：成功自动关窗 / 失败留在窗口里（上传接口用假响应，不会真传文件）；
  4. 加 --with-generate 时真点一次「生成文案」：会真调视觉模型、真写库，
     并且**会把那条素材改名成标题**（文件名只留中文和数字）——请挑一条不在乎名字的素材。

本脚本只覆盖素材管理页；背景音乐页与「合成背景音乐」全链路在
`tools/verify_bgm_page.py`（那个脚本自带隔离沙箱，不会碰真实数据）。

前置：前端 Vite（5173）与后端 Flask（5409）都已启动（./start.sh），
      并且已在「模型设置」里配好模型——否则第 3 步必然失败。
      登录密码读自 settings.json 的 auth.password，脚本会自己走一遍登录页。

用法：
    .venv/bin/python tools/verify_material_page.py
    .venv/bin/python tools/verify_material_page.py --with-generate
    .venv/bin/python tools/verify_material_page.py --base http://127.0.0.1:5175   # 换一个前端地址
"""

import json
import re
import sys
import tempfile
from pathlib import Path

# 脚本在 tools/ 下，仓库根要进 sys.path 才能 import conf（它会设置 PLAYWRIGHT_BROWSERS_PATH）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conf import BASE_DIR  # noqa: F401,E402  # 导入它会把 PLAYWRIGHT_BROWSERS_PATH 指到项目内的 .browsers/
from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:5173"
if "--base" in sys.argv:
    BASE = sys.argv[sys.argv.index("--base") + 1]
URL = f"{BASE}/#/material-management"
SHOT_DIR = Path("screenshots")
SHOT_DIR.mkdir(exist_ok=True)

VIEWPORT = {"width": 1440, "height": 950}

failures: list[str] = []


def read_credentials() -> tuple[str, str]:
    """从 settings.json 读后台登录账号密码。

    密码是首次启动后端时随机生成的（见 myUtils/web_auth.py）。
    这里不写死、也不问用户，直接读那份文件——脚本本来就跑在同一个仓库里。
    """
    from myUtils.app_settings import SETTINGS_PATH  # 延迟导入：只在这里用到

    if not Path(SETTINGS_PATH).is_file():
        raise SystemExit(f"找不到 {SETTINGS_PATH}：先启动一次后端（./start.sh），它会生成随机密码")
    cfg = json.loads(Path(SETTINGS_PATH).read_text(encoding="utf-8"))
    auth = cfg.get("auth") or {}
    password = (auth.get("password") or "").strip()
    if not password:
        raise SystemExit(f"{SETTINGS_PATH} 里没有 auth.password：启动一次后端会自动补上")
    return (auth.get("username") or "admin").strip(), password


def login_ui(browser):
    """真走一遍登录页，返回可复用的 `storage_state`。

    整个后台都要登录（前端路由守卫 + 后端 before_request 双重拦截）。
    脚本若直接 goto 素材页，会被守卫赶到登录页，后面每条断言都会在
    "找不到 .material-list" 上失败——所以必须先登录。

    顺带就把登录页本身验了：填不上 / 点不动 / 跳不走，这里会直接超时报错。
    """
    username, password = read_credentials()
    context = browser.new_context(viewport=VIEWPORT)
    page = context.new_page()

    page.goto(f"{BASE}/#/login", wait_until="networkidle")
    page.fill('input[autocomplete="username"]', username)
    page.fill('input[autocomplete="current-password"]', password)
    page.click(".login-button")
    # 登录成功后跳首页。hash 路由等 URL 不可靠，等登录后才有的侧边栏更稳。
    page.wait_for_selector(".sidebar-menu", timeout=15000)

    state = context.storage_state()  # 含 localStorage 里的 token
    context.close()
    return state


def authed_page(browser, storage):
    """开一个带着登录态的新页面（等价于原来的 browser.new_page(viewport=...)）。"""
    context = browser.new_context(storage_state=storage, viewport=VIEWPORT)
    return context.new_page()


def check(cond, msg):
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        failures.append(msg)


def rows_of(page):
    return page.locator(".material-list .el-table__body-wrapper tbody tr")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        print("\n[0] 登录（账号 admin，密码读自 settings.json 的 auth.password）")
        storage = login_ui(browser)
        print("  ✅ 已登录，登录态将在下面的页面里复用")

        page = authed_page(browser, storage)
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        # ---------------- 1) 真实数据 ----------------
        print("\n[1] 真实数据：分页 + AI 文案列 + 操作按钮")
        page.goto(URL, wait_until="networkidle")
        page.wait_for_selector(".material-list .el-table__row", timeout=15000)
        page.wait_for_timeout(500)

        row_count = rows_of(page).count()
        check(row_count <= 20, f"一页最多 20 条（当前 {row_count} 条）")

        # 上传时间倒序
        # 列位置（2026-09-30 起，见下面的列头断言）：1 文件名 2 大小 3 上传时间 4 AI 文案
        # 5 背景音乐 6 操作。所以时间在第 3 列、AI 文案在第 4 列——UUID 列已经去掉了。
        times = page.locator(".material-list .el-table__row td:nth-child(3)").all_inner_texts()
        check(times == sorted(times, reverse=True), f"上传时间倒序：{times[:3]} …")

        # AI 文案列
        tags = page.locator(".material-list .el-table__row td:nth-child(4)").all_inner_texts()
        check(all(t.strip() in ("已有", "无") for t in tags), f"AI 文案列取值正常：{tags}")

        # 列头：UUID 列已去掉，「背景音乐」列紧跟「AI 文案」
        headers = [h.strip() for h in page.locator(".material-list .el-table__header th").all_inner_texts()]
        check("UUID" not in headers, f"没有 UUID 列（当前列：{' / '.join(headers)}）")
        check(
            "背景音乐" in headers and headers.index("背景音乐") == headers.index("AI 文案") + 1,
            "「背景音乐」列排在「AI 文案」之后",
        )
        bgm_tags = page.locator(".material-list .el-table__row td:nth-child(5)").all_inner_texts()
        check(
            all(t.strip() in ("待合成", "合成中", "已合成") for t in bgm_tags),
            f"背景音乐列取值正常：{bgm_tags}",
        )

        # 按钮：有文案 → 预览文案；无文案 → 生成文案
        gen = page.locator(".material-list button:has-text('生成文案')")
        prev_copy = page.locator(".material-list button:has-text('预览文案')")
        n_gen, n_prev = gen.count(), prev_copy.count()
        n_has = sum(1 for t in tags if t.strip() == "已有")
        check(
            n_prev == n_has and n_gen == row_count - n_has,
            f"按钮数量匹配：已有文案 {n_has} 行→预览文案 {n_prev} 个，无文案 {row_count - n_has} 行→生成文案 {n_gen} 个",
        )

        # 分页组件
        total_text = page.locator(".material-pagination").inner_text()
        check(("共" in total_text or "Total" in total_text) and str(row_count) in total_text, f"分页显示总数：{total_text.splitlines()[0]}")

        # 搜索框 + 刷新仍在
        check(page.locator(".material-search input").count() == 1, "搜索框存在")
        check(page.locator("button:has-text('上传素材')").count() == 1, "上传素材按钮存在")

        page.screenshot(path=str(SHOT_DIR / "material_page.png"), full_page=True)

        # 预览文案弹窗
        if n_prev:
            prev_copy.first.click()
            page.wait_for_selector(".copy-preview", timeout=5000)
            dialog = page.locator(".copy-preview").inner_text()
            check("标题" in dialog and "正文" in dialog and "话题" in dialog, "预览文案弹窗含 标题/正文/话题")
            check(page.locator(".el-dialog:has(.copy-preview) button:has-text('重新生成')").count() == 1, "弹窗内有重新生成按钮")
            page.screenshot(path=str(SHOT_DIR / "material_copy_dialog.png"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)

        # 搜索过滤 + 结果为空
        page.fill(".material-search input", "ZZZ-不存在的素材")
        page.wait_for_timeout(400)
        check(rows_of(page).count() == 0, "搜索无结果时表格为空")
        page.fill(".material-search input", "")
        page.wait_for_timeout(400)
        check(rows_of(page).count() == row_count, "清空搜索后恢复全部")

        # ---------------- 2) 假数据：验证 20 条/页 ----------------
        print("\n[2] 抓包替换为 45 条素材，验证 20 条/页 分页")
        fake = []
        for i in range(45):
            name = f"fake-{i:02d}.mp4"
            fake.append(
                {
                    "id": 900 + i,
                    "filename": name,
                    "file_path": f"uuid{i:04d}-0000-0000-0000-000000000000_{name}",
                    "uuid": f"uuid{i:04d}-0000-0000-0000-000000000000",
                    "filesize": 1.5,
                    "upload_time": f"2026-09-{25 - i // 10:02d} {23 - i % 10:02d}:00:00",
                    "has_copy": i % 3 == 0,
                    "title": f"标题{i}" if i % 3 == 0 else "",
                    "desc": f"正文{i}" if i % 3 == 0 else "",
                    "tags": ["测试"] if i % 3 == 0 else [],
                    "copy_updated_at": "2026-09-25 12:00:00" if i % 3 == 0 else None,
                }
            )

        page2 = authed_page(browser, storage)
        page2.route(
            "**/getFiles*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"code": 200, "msg": "success", "data": fake}, ensure_ascii=False),
            ),
        )
        page2.goto(URL, wait_until="networkidle")
        page2.wait_for_selector(".material-list .el-table__row", timeout=15000)
        page2.wait_for_timeout(400)
        check(rows_of(page2).count() == 20, f"第 1 页 20 条（实际 {rows_of(page2).count()}）")

        page2.locator(".material-pagination .el-pager li", has_text="2").first.click()
        page2.wait_for_timeout(400)
        check(rows_of(page2).count() == 20, f"第 2 页 20 条（实际 {rows_of(page2).count()}）")

        page2.locator(".material-pagination .el-pager li", has_text="3").first.click()
        page2.wait_for_timeout(400)
        check(rows_of(page2).count() == 5, f"第 3 页 5 条（实际 {rows_of(page2).count()}）")
        page2.screenshot(path=str(SHOT_DIR / "material_page_p3.png"), full_page=True)

        # 搜索后自动回到第 1 页
        page2.fill(".material-search input", "fake-1")
        page2.wait_for_timeout(400)
        active = page2.locator(".material-pagination .el-pager li.is-active").inner_text()
        check(active.strip() == "1", f"搜索后回到第 1 页（当前 {active.strip()}）")

        # ---------------- 3) 上传：成功后自动关窗 ----------------
        print("\n[3] 上传素材：成功自动关窗、失败留在窗口里（上传接口用假响应）")
        tmp_upload = Path(tempfile.gettempdir()) / "verify-material-upload.mp4"
        tmp_upload.write_bytes(b"\x00" * 2048)
        try:
            page.goto(URL, wait_until="networkidle")
            page.wait_for_selector(".material-list .el-table__row", timeout=15000)
            hits: list[str] = []
            page.on("request", lambda r: hits.append(r.url) if "uploadSave" in r.url else None)

            ok_body = json.dumps({"code": 200, "msg": "ok", "data": {"filename": "x", "filepath": "x"}})
            page.route("**/uploadSave", lambda r: r.fulfill(status=200, content_type="application/json", body=ok_body))
            page.click("button:has-text('上传素材')")
            page.wait_for_selector(".el-dialog:visible:has-text('确认上传')")
            page.set_input_files(".el-dialog input[type=file]", str(tmp_upload))
            page.wait_for_timeout(400)
            check(page.locator(".upload-file-item").count() == 1, "文件已加入上传列表")
            page.click(".el-dialog button:has-text('确认上传')")
            page.wait_for_timeout(1800)
            check(page.locator(".el-dialog:visible:has-text('确认上传')").count() == 0, "上传成功后自动关闭窗口（不会再点一次传重复文件）")
            check(len(hits) == 1, f"只发了一次上传请求（实际 {len(hits)} 次）")

            page.click("button:has-text('上传素材')")
            page.wait_for_selector(".el-dialog:visible:has-text('确认上传')")
            page.wait_for_timeout(500)
            check(page.locator(".upload-file-item").count() == 0, "重新打开窗口时文件列表已清空")

            # 失败分支：换一个文件名再选——同一路径重复选浏览器不触发 change，那是浏览器行为
            tmp_upload2 = Path(tempfile.gettempdir()) / "verify-material-upload-2.mp4"
            tmp_upload2.write_bytes(b"\x00" * 2048)
            err_body = json.dumps({"code": 500, "msg": "模拟上传失败", "data": None}, ensure_ascii=False)
            page.route("**/uploadSave", lambda r: r.fulfill(status=500, content_type="application/json", body=err_body))
            page.set_input_files(".el-dialog input[type=file]", str(tmp_upload2))
            page.wait_for_timeout(600)
            page.click(".el-dialog button:has-text('确认上传')")
            page.wait_for_timeout(2000)
            check(page.locator(".el-dialog:visible:has-text('确认上传')").count() == 1, "上传失败时窗口保持打开")
            check(page.locator(".upload-file-item").count() == 1, "失败的文件留在列表里，可直接重试")
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            page.unroute("**/uploadSave")
            tmp_upload2.unlink(missing_ok=True)
        finally:
            tmp_upload.unlink(missing_ok=True)

        # ---------------- 4) 真实生成文案（调模型，素材会被改名） ----------------
        if "--with-generate" in sys.argv:
            print("\n[4] 真实点击「生成文案」（调用视觉模型，素材会按标题改名）")
            page.goto(URL, wait_until="networkidle")
            page.wait_for_selector(".material-list .el-table__row", timeout=15000)
            btn = page.locator(".material-list button:has-text('生成文案')").first
            row = page.locator(".material-list .el-table__row", has=page.locator("button:has-text('生成文案')")).first
            name = row.locator("td:nth-child(2)").inner_text()
            print(f"  目标素材：{name}")
            btn.click()
            page.wait_for_selector(".copy-preview", timeout=240000)
            text = page.locator(".copy-preview").inner_text()
            title = page.locator(".copy-preview .copy-title").inner_text().strip()
            print("  --- 生成结果 ---")
            print("\n".join("  " + ln for ln in text.splitlines()))
            page.screenshot(path=str(SHOT_DIR / "material_generate_result.png"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(900)
            after = page.locator(".material-list .el-table__row td:nth-child(4)").all_inner_texts()
            check("已有" in after, "生成后该行 AI 文案列变为「已有」")
            new_name = page.locator(".material-list .el-table__row td:nth-child(2)").first.inner_text().strip()
            check(
                bool(re.fullmatch(r"[\u4e00-\u9fff0-9]+\.[A-Za-z0-9]+", new_name)),
                f"素材已按标题改名，且只含中文和数字：{new_name}",
            )
            check(new_name != name.strip(), f"文件名确实变了（{name.strip()} → {new_name}，标题「{title}」）")
            page.screenshot(path=str(SHOT_DIR / "material_page_after_generate.png"), full_page=True)

        print("\n[控制台错误]")
        # 故意造的上传失败会打 console.error，这种不算问题
        noise = ("favicon", "500 (Internal Server Error)", "status code 500", "verify-material-upload")
        real_errors = [e for e in errors if not any(n in e for n in noise)]
        for e in real_errors[:10]:
            print("  ⚠️", e[:200])
        check(not real_errors, "浏览器控制台无报错")

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
