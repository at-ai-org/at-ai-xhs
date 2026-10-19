# -*- coding: utf-8 -*-
"""素材管理页验收脚本（Playwright 驱动真实浏览器）。

检查项：
  1. 真实数据：按上传时间倒序、一页最多 20 条、AI 文案列「已有/无」、
     「背景音乐」列（待合成/合成中/已合成，紧跟 AI 文案、UUID 列已去掉）、
     有文案→「预览文案」、无文案→「生成文案」、预览弹窗、搜索重置页码；
  2. 抓包替换成 45 条假素材，验证 20 条/页 的翻页行为；
  3. 上传：成功自动关窗 / 失败留在窗口里（上传接口用假响应，不会真传文件）；
  4. 批量操作：多选（含翻页保留）→「批量生成文案」/「批量合成背景音乐」。
     **接口全部用假响应**（getFiles / xhs/analyze / music/* 都被 route 接管），
     所以既不真调模型、也不真跑 ffmpeg、更不碰真实素材；
  5. 加 --with-generate 时真点一次「生成文案」：会真调视觉模型、真写库，
     并且**会把那条素材改名成标题**（文件名只留中文和数字）——请挑一条不在乎名字的素材。

本脚本只覆盖素材管理页；背景音乐页与「合成背景音乐」全链路在
`tools/verify_bgm_page.py`（那个脚本自带隔离沙箱，不会碰真实数据）。

前置：前端 Vite（5173）与后端 Flask（5409）都已启动（./start.sh），
      并且已在「模型设置」里配好模型——否则第 5 步必然失败。
      登录密码读自 settings.json 的 auth.password，脚本会自己走一遍登录页。

用法：
    .venv/bin/python tools/verify_material_page.py
    .venv/bin/python tools/verify_material_page.py --with-generate
    .venv/bin/python tools/verify_material_page.py --base http://127.0.0.1:5175   # 换一个前端地址

注：Vite 只监听 localhost（IPv6），默认的 127.0.0.1 连不上时用
    `--base http://localhost:5173`。
"""

import json
import re
import sys
import tempfile
import time
from pathlib import Path

# 脚本在 tools/ 下，仓库根要进 sys.path 才能 import conf（它会设置 PLAYWRIGHT_BROWSERS_PATH）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conf import BASE_DIR  # noqa: F401,E402  # 导入它会把 PLAYWRIGHT_BROWSERS_PATH 指到项目内的 .browsers/
import myUtils.publish_evidence as evidence  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:5173"
if "--base" in sys.argv:
    BASE = sys.argv[sys.argv.index("--base") + 1]
URL = f"{BASE}/#/material-management"
# 截图落点统一由 myUtils/publish_evidence 决定：发布失败的现场在
# `screenshots/<平台>/`，验收脚本的图放 `screenshots/tools/`（免得混着看）。
SHOT_DIR = evidence.platform_dir("tools")

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
        # 列位置（2026-10 起：第 1 列是**多选勾选框**，见下面的批量操作）：
        # 1 勾选 2 文件名 3 大小 4 上传时间 5 AI 文案 6 背景音乐 7 操作。
        # 所以时间在第 4 列、AI 文案在第 5 列——UUID 列已经去掉了。
        times = page.locator(".material-list .el-table__row td:nth-child(4)").all_inner_texts()
        check(times == sorted(times, reverse=True), f"上传时间倒序：{times[:3]} …")

        # AI 文案列
        tags = page.locator(".material-list .el-table__row td:nth-child(5)").all_inner_texts()
        check(all(t.strip() in ("已有", "无") for t in tags), f"AI 文案列取值正常：{tags}")

        # 列头：UUID 列已去掉，「背景音乐」列紧跟「AI 文案」；多选框在最左边
        headers = [h.strip() for h in page.locator(".material-list .el-table__header th").all_inner_texts()]
        check("UUID" not in headers, f"没有 UUID 列（当前列：{' / '.join(headers)}）")
        check(
            "背景音乐" in headers and headers.index("背景音乐") == headers.index("AI 文案") + 1,
            "「背景音乐」列排在「AI 文案」之后",
        )
        check(
            page.locator(".material-list .el-table__header th:first-child .el-checkbox").count() == 1,
            "第 1 列是多选框（表头全选）",
        )
        bgm_tags = page.locator(".material-list .el-table__row td:nth-child(6)").all_inner_texts()
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

        # ---------------- 1b) 素材预览：媒体必须真的加载出来 ----------------
        #
        # 回归测试：预览走的是 <video src> / <img src>，**带不了请求头**，
        # 所以地址上必须挂 token（utils/session.js 的 withToken）——后端的
        # before_request 守卫只放行页面外壳和静态资源，其余一律要么请求头、
        # 要么查询串带 token，否则 401。
        #
        # 漏挂的表现很难自己暴露出来：<video> 里的 <source> 加载失败时 error 事件
        # 打在 <source> 上、不冒泡到 <video>，onMediaError 根本不触发，
        # 于是"转圈转到天荒地老"，连一句失败提示都没有。
        print("\n[1b] 素材预览：媒体真的加载出来了吗")
        media_status: list[int] = []
        page.on("response", lambda r: media_status.append(r.status) if "/getFile" in r.url else None)

        video_row = page.locator(
            ".material-list .el-table__row", has=page.locator("button:has-text('背景音乐')")
        ).first
        if video_row.count() == 0:
            check(False, "素材库里没有视频，预览没法验（先上传一个视频再跑）")
        else:
            video_name = video_row.locator("td:nth-child(2)").inner_text().strip()
            video_row.get_by_role("button", name="预览", exact=True).click()
            page.wait_for_selector(".el-dialog:visible video", timeout=8000)

            state = {}
            deadline = time.time() + 15
            # 等**两个条件都满足**：出帧 + 转圈消失。
            # 只看出帧不够——转圈还在就是用户看到的"半天出不来"；
            # 也不能出帧就立刻判定转圈：Element Plus 的 loading 遮罩是渐变淡出的，
            # 得留它几百毫秒，否则会误报（这一条一开始就是这么误报的）。
            while time.time() < deadline:
                state = page.evaluate(
                    """() => {
                        const v = document.querySelector('.el-dialog video');
                        if (!v) return {};
                        const mask = document.querySelector('.el-dialog .el-loading-mask');
                        return {
                            readyState: v.readyState,
                            width: v.videoWidth,
                            src: v.currentSrc || (v.querySelector('source') || {}).src || '',
                            spinner: mask ? getComputedStyle(mask).display !== 'none' : false,
                            errText: (document.querySelector('.el-dialog .el-alert__title') || {}).textContent || '',
                        };
                    }"""
                )
                if state.get("errText"):
                    break
                if state.get("readyState", 0) >= 2 and not state.get("spinner"):
                    break
                page.wait_for_timeout(200)

            print(f"  预览的素材：{video_name}")
            print(f"  /getFile 状态码：{media_status or '（没有请求！）'}")
            check(
                not any(s == 401 for s in media_status) and any(s in (200, 206) for s in media_status),
                f"预览地址带上了登录态（/getFile 状态码 {media_status or '无请求'}——401 就是地址没挂 token）",
            )
            check(
                state.get("readyState", 0) >= 2,
                f"视频真的出帧了（readyState={state.get('readyState')}，画面 {state.get('width')}px）",
            )
            check(not state.get("spinner"), "转圈已经消失（不会一直转）")
            page.screenshot(path=str(SHOT_DIR / "material_video_preview.png"))

            # 图片预览走的是同一套地址拼法，顺手验一个
            image_row = page.locator(
                ".material-list .el-table__row", has=page.locator("td:has-text('.jpg')")
            ).first
            if image_row.count() == 0:
                image_row = page.locator(
                    ".material-list .el-table__row", has=page.locator("td:has-text('.png')")
                ).first
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)

            if image_row.count() == 0:
                print("  （素材库里没有图片，跳过图片预览这一条）")
            else:
                media_status.clear()
                image_row.get_by_role("button", name="预览", exact=True).click()
                page.wait_for_selector(".el-dialog:visible img", timeout=8000)
                img_ok = False
                deadline = time.time() + 10
                while time.time() < deadline:
                    img_ok = page.evaluate(
                        """() => {
                            const i = document.querySelector('.el-dialog img');
                            return !!i && i.complete && i.naturalWidth > 0;
                        }"""
                    )
                    if img_ok:
                        break
                    page.wait_for_timeout(200)
                check(
                    img_ok and any(s in (200, 206) for s in media_status),
                    f"图片预览也出来了（/getFile 状态码 {media_status or '无请求'}）",
                )
                page.keyboard.press("Escape")
                page.wait_for_timeout(400)

            # 既不是视频也不是图片的文件（素材库里有个 .dat）：弹窗走的是
            # "文件信息 + 下载按钮"那一支，没有 media 元素可以等 loadeddata/load——
            # 转圈必须当场收掉，否则它会一直盖在文件信息上，又是一个"预览出不来"。
            other_index = -1
            media_exts = (
                ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".flv", ".wmv",
                ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
            )
            for i in range(rows_of(page).count()):
                row_name = rows_of(page).nth(i).locator("td:nth-child(2)").inner_text().strip()
                if not row_name.lower().endswith(media_exts):
                    other_index = i
                    break
            if other_index < 0:
                print("  （素材库里没有非视频/非图片的文件，跳过这一条）")
            else:
                other_row = rows_of(page).nth(other_index)
                other_row.get_by_role("button", name="预览", exact=True).click()
                page.wait_for_selector(".el-dialog:visible .file-info", timeout=8000)
                page.wait_for_timeout(600)
                other_state = page.evaluate(
                    """() => {
                        const d = document.querySelector('.el-dialog');
                        const mask = d.querySelector('.el-loading-mask');
                        return {
                            spinner: mask ? getComputedStyle(mask).display !== 'none' : false,
                            info: !!d.querySelector('.file-info'),
                            hasMedia: !!d.querySelector('video,img'),
                        };
                    }"""
                )
                check(
                    other_state.get("info") and not other_state.get("hasMedia"),
                    "非视频/非图片素材显示的是文件信息（没有播放器）",
                )
                check(
                    not other_state.get("spinner"),
                    "非视频/非图片素材不会一直转圈（没有 media 事件可等，转圈得当场收掉）",
                )

                # 「下载文件」用的是同一条 /getFile 地址（window.open 也一样带不了请求头），
                # 所以顺手验一下它也被鉴权放行了——这条以前是 401，点了只会静默失败。
                try:
                    with page.context.expect_event(
                        "response",
                        predicate=lambda r: "/getFile" in r.url,
                        timeout=8000,
                    ) as resp_info:
                        page.click(".el-dialog:visible .file-info button:has-text('下载文件')")
                    dl_status = resp_info.value.status
                    check(dl_status in (200, 206), f"下载也带上了登录态（/getFile 状态码 {dl_status}）")
                except Exception as exc:  # noqa: BLE001 —— 下载行为本身依浏览器而定，别把整轮拖垮
                    print(f"  （下载这一条没测成：{type(exc).__name__}）")
                for extra in page.context.pages[1:]:
                    extra.close()

                page.keyboard.press("Escape")
                page.wait_for_timeout(400)

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

        # ---------------- 4) 批量操作（多选 + 批量生成文案 / 批量合成背景音乐） ----------------
        print("\n[4] 批量操作：多选（含翻页保留）→ 批量生成文案 / 批量合成背景音乐")
        print("    接口全部用假响应：不调模型、不跑 ffmpeg、不碰真实素材")

        # 25 条假素材：够翻两页（每页 20），同时有视频和图片。
        # upload_time 越大越新 → 第一页是 i=24…5，第二页是 i=4…0。
        # i % 7 == 0 的那几条"已有文案"，用来验「跳过已有文案」。
        def fake_material(i):
            is_video = i < 23
            name = f"假素材{i:02d}.mp4" if is_video else f"假图片{i:02d}.jpg"
            has_copy = i % 7 == 0
            return {
                "id": 700 + i,
                "filename": name,
                "file_path": f"uuid{i:04d}-0000-0000-0000-000000000000_{name}",
                "uuid": f"uuid{i:04d}-0000-0000-0000-000000000000",
                "filesize": 1.5,
                "upload_time": f"2026-10-{1 + i // 10:02d} {i % 10:02d}:00:00",
                "has_copy": has_copy,
                "title": f"旧标题{i}" if has_copy else "",
                "desc": f"旧正文{i}" if has_copy else "",
                "tags": ["旧"] if has_copy else [],
                "copy_updated_at": "2026-10-01 12:00:00" if has_copy else None,
                "bgm_status": "pending",
                "bgm_status_text": "待合成",
                "bgm_music": "",
                "bgm_progress": 0,
                "bgm_message": "",
            }

        mats = [fake_material(i) for i in range(25)]

        def fulfill(route, payload, status=200):
            route.fulfill(
                status=status,
                content_type="application/json",
                body=json.dumps(payload, ensure_ascii=False),
            )

        def find_mat(disk):
            for m in mats:
                if m["file_path"] == disk or m["file_path"].endswith("/" + str(disk)):
                    return m
            return None

        # 请求日志：用来验"生成文案是串行的""合成最多 2 条同时在跑"。
        # 事件按浏览器真实发生顺序追加，所以顺序本身就是证据。
        log: list[tuple[str, str]] = []
        analyze_calls: list[str] = []
        compose_calls: list[str] = []
        cancel_calls: list[str] = []
        tasks: dict[str, dict] = {}
        # 前几次轮询都回"合成中"，好让「中断」那一段有可点的窗口
        keep_running = {"n": 2}
        #: 让接下来的 N 次 analyze 返回失败（验"一条失败不带停整批"）
        fail_next = {"n": 0}

        def on_analyze(route):
            """假 analyze：按标题改名（保留扩展名，和真后端 `_rename_material_to_title` 一致）。"""
            payload = json.loads(route.request.post_data or "{}")
            disk = payload.get("video") or ""
            analyze_calls.append(disk)
            if fail_next["n"] > 0:
                # 模拟"这一条生成失败"（比如模型没配好）：批量不能因此整批停下
                fail_next["n"] -= 1
                fulfill(route, {"code": 500, "msg": "模拟生成失败：还没有配置模型服务地址"}, status=500)
                return
            n = len(analyze_calls)
            title = f"新标题{n}"
            mat = find_mat(disk)
            if mat:
                # 真后端会把素材改名成标题，并把文案落到素材上
                ext = Path(mat["file_path"]).suffix or ".mp4"
                mat["filename"] = f"{title}{ext}"
                mat["file_path"] = f"{title}{ext}"
                mat["has_copy"] = True
                mat["title"] = title
                mat["desc"] = f"新正文{n}"
                mat["tags"] = ["新"]
            fulfill(route, {"code": 200, "msg": "生成成功", "data": {
                "title": title,
                "desc": f"新正文{n}",
                "tags": ["新"],
                "video_name": mat["file_path"] if mat else disk,
                "rename_note": "",
            }})

        def on_compose(route):
            payload = json.loads(route.request.post_data or "{}")
            compose_calls.append(payload.get("material") or "")
            task_id = f"t{len(compose_calls)}"
            tasks[task_id] = {"material": payload.get("material") or "", "polls": 0}
            fulfill(route, {"code": 200, "msg": "ok", "data": {
                "task_id": task_id, "material": payload.get("material") or "",
            }})

        def on_task(route):
            task_id = route.request.url.split("?")[0].rstrip("/").split("/")[-1]
            task = tasks.get(task_id)
            if not task:
                fulfill(route, {"code": 404, "msg": "任务不存在", "data": None}, status=404)
                return
            task["polls"] += 1
            base = {"task_id": task_id, "material": task["material"], "output": ""}
            if task["polls"] <= keep_running["n"]:
                fulfill(route, {"code": 200, "msg": "ok", "data": {
                    **base, "state": "running", "progress": 40, "message": "正在合成…"}})
                return
            # 成品名以**展示名**为基准（真后端就是这么算的：上传时磁盘名带 uuid 前缀，
            # 展示名不带，合成后磁盘名和展示名变成同一个字符串）
            mat = find_mat(task["material"])
            ext = Path(task["material"]).suffix or ".mp4"
            out = Path(mat["filename"] if mat else task["material"]).stem + "-轻音乐" + ext
            if mat:
                mat["filename"] = out
                mat["file_path"] = out
                mat["bgm_status"] = "done"
                mat["bgm_status_text"] = "已合成"
                mat["bgm_music"] = "轻音乐"
                mat["bgm_progress"] = 100
            fulfill(route, {"code": 200, "msg": "ok", "data": {
                **base, "state": "done", "progress": 100,
                "message": f"合成完成：{out}", "output": out}})

        def on_cancel(route):
            cancel_calls.append(route.request.url.split("?")[0])
            fulfill(route, {"code": 200, "msg": "ok", "data": {}})

        page4 = authed_page(browser, storage)
        page4.on("request", lambda r: log.append(("req", r.url.split("?")[0])))
        page4.on("response", lambda r: log.append(("resp", r.url.split("?")[0])))
        for pattern, handler in (
            ("**/getFiles*", lambda r: fulfill(r, {"code": 200, "msg": "success", "data": mats})),
            ("**/xhs/analyze", on_analyze),
            ("**/music/list", lambda r: fulfill(r, {"code": 200, "msg": "ok", "data": [
                {"id": 1, "name": "轻音乐", "duration": 30}]})),
            ("**/music/statuses", lambda r: fulfill(r, {"code": 200, "msg": "ok", "data": {}})),
            ("**/music/compose", on_compose),
            ("**/music/task/*/cancel", on_cancel),
            ("**/music/task/*", on_task),
        ):
            page4.route(pattern, handler)

        def batch_dialog(title):
            return page4.locator(".el-dialog:visible").filter(
                has=page4.locator(".el-dialog__title", has_text=title)
            )

        def items_in(dialog):
            return dialog.locator(".batch-task-item")

        def item_texts(dialog):
            return [t.strip() for t in items_in(dialog).all_inner_texts()]

        def batch_btn(title):
            """工具栏里的批量按钮（和「上传素材」并排，见 4a 的置灰断言）。"""
            return page4.locator(f".action-buttons button:has-text('{title}')")

        def selected_hint():
            return page4.locator(".material-search .selected-hint").inner_text().strip()

        def open_batch_dialog(title, row_indexes):
            """勾选指定行 → 打开对应的批量弹窗。"""
            page4.locator(".material-list .el-table__row").nth(row_indexes[0]).locator(".el-checkbox").click()
            for idx in row_indexes[1:]:
                page4.locator(".material-list .el-table__row").nth(idx).locator(".el-checkbox").click()
            page4.wait_for_timeout(300)
            batch_btn(title).click()
            page4.wait_for_selector(".el-dialog:visible .batch-task-item", timeout=8000)
            page4.wait_for_timeout(300)

        # ---- 4a) 多选 + 翻页保留 ----
        page4.goto(URL, wait_until="networkidle")
        page4.wait_for_selector(".material-list .el-table__row", timeout=15000)
        page4.wait_for_timeout(400)
        check(
            page4.locator(".material-search .action-buttons button:has-text('上传素材')").count() == 1
            and batch_btn("批量生成文案").count() == 1,
            "两个批量按钮就在「上传素材」旁边（同一行工具栏）",
        )
        check(
            batch_btn("批量生成文案").is_disabled() and batch_btn("批量合成背景音乐").is_disabled(),
            "没勾选素材时两个批量按钮都置灰",
        )
        check(selected_hint() == "已选 0 项", f"没勾选时显示「已选 0 项」（读到 {selected_hint()}）")

        # 第一页勾 2 条（i=24 图片无文案、i=21 视频已有文案）
        page4.locator(".material-list .el-table__row").nth(0).locator(".el-checkbox").click()
        page4.locator(".material-list .el-table__row").nth(3).locator(".el-checkbox").click()
        page4.wait_for_timeout(300)
        check(selected_hint() == "已选 2 项", f"勾选后显示已选数量（读到 {selected_hint()}）")
        check(
            batch_btn("批量生成文案").is_enabled() and batch_btn("批量合成背景音乐").is_enabled(),
            "勾选了素材，两个批量按钮都能点",
        )

        # 翻到第二页再勾 1 条（i=3 视频无文案）
        page4.locator(".material-pagination .el-pager li", has_text="2").first.click()
        page4.wait_for_timeout(400)
        page4.locator(".material-list .el-table__row").nth(1).locator(".el-checkbox").click()
        page4.wait_for_timeout(300)
        page4.locator(".material-pagination .el-pager li", has_text="1").first.click()
        page4.wait_for_timeout(400)
        check(
            selected_hint() == "已选 3 项",
            f"翻页后勾选保留（第二页勾的第 3 条回到第一页仍在，读到 {selected_hint()}）",
        )

        # ---- 4b) 批量生成文案：串行、跳已有文案、改名后列表跟得上 ----
        batch_btn("批量生成文案").click()
        page4.wait_for_selector(".el-dialog:visible .batch-task-item", timeout=8000)
        page4.wait_for_timeout(300)
        dlg = batch_dialog("批量生成文案")
        check(items_in(dlg).count() == 3, f"弹窗列出了 3 条待生成素材（实际 {items_in(dlg).count()}）")
        check(
            "已有文案 1 条" in dlg.inner_text(),
            "弹窗提示了这一批里有多少条已有文案",
        )
        check(
            "按设置跳过" in " ".join(item_texts(dlg)),
            "默认勾选「跳过已有 AI 文案」：已有文案那条显示为已跳过",
        )

        page4.click(".el-dialog:visible button:has-text('开始生成')")
        # 等这一批跑完（按钮从「中断」回到「关闭」）
        page4.wait_for_selector(".el-dialog:visible button:has-text('关闭')", timeout=30000)
        page4.wait_for_timeout(600)

        check(len(analyze_calls) == 2, f"只对 2 条发起了生成（跳过的那条没发请求，实际 {len(analyze_calls)}）")
        done_items = [t for t in item_texts(dlg) if "已完成" in t]
        check(len(done_items) == 2, f"2 条显示已完成（实际 {len(done_items)}）")
        check(
            len([t for t in item_texts(dlg) if "已跳过" in t]) == 1,
            "已有文案的那条保持已跳过",
        )

        analyze_reqs = [i for i, (k, u) in enumerate(log) if k == "req" and u.endswith("/xhs/analyze")]
        analyze_resps = [i for i, (k, u) in enumerate(log) if k == "resp" and u.endswith("/xhs/analyze")]
        before_first_resp = sum(1 for i in analyze_reqs if i < analyze_resps[0])
        check(
            before_first_resp == 1,
            f"生成文案是串行的：第一条响应之前只发出 1 条请求（实际 {before_first_resp}）",
        )
        check(
            selected_hint() == "已选 0 项"
            and batch_btn("批量生成文案").is_disabled()
            and batch_btn("批量合成背景音乐").is_disabled(),
            "批次开始后自动清空勾选、两个批量按钮回到置灰"
            "（素材会改名，留着旧勾选会指向不存在的文件）",
        )
        page4.keyboard.press("Escape")
        page4.wait_for_timeout(500)

        # 改名 + 新文案要落到列表上（就地改行 + 收尾整表刷新）
        names = page4.locator(".material-list .el-table__row td:nth-child(2)").all_inner_texts()
        check(
            any(n.strip().startswith("新标题") for n in names),
            f"生成后列表里显示的是改名后的素材：{names[:3]}",
        )
        copy_col = page4.locator(".material-list .el-table__row td:nth-child(5)").all_inner_texts()
        check("已有" in copy_col, "生成后 AI 文案列变为「已有」")
        # 图片素材也被改名了，但扩展名要保住（不然会被当成视频，操作列会多出「背景音乐」）
        img_row = page4.locator(".material-list .el-table__row", has_text=".jpg").first
        check(img_row.count() == 1, "图片素材改名后仍是 .jpg（扩展名没被吃掉）")

        # ---- 4c) 批量合成背景音乐：只跑视频、图片跳过、最多 2 条同时跑 ----
        page4.goto(URL, wait_until="networkidle")
        page4.wait_for_selector(".material-list .el-table__row", timeout=15000)
        page4.wait_for_timeout(400)

        # 只勾一张图片：生成文案照常，换音轨置灰（图片没有音轨可换）
        page4.locator(".material-list .el-table__row").nth(0).locator(".el-checkbox").click()
        page4.wait_for_timeout(300)
        check(batch_btn("批量生成文案").is_enabled(), "只勾图片时「批量生成文案」仍可点")
        check(batch_btn("批量合成背景音乐").is_disabled(), "只勾图片时「批量合成背景音乐」置灰")
        page4.locator(".material-list .el-table__row").nth(0).locator(".el-checkbox").click()
        page4.wait_for_timeout(200)

        # 第一页前 4 行 = i=24/23（图片）+ i=22/21（视频）
        open_batch_dialog("批量合成背景音乐", [0, 1, 2, 3])
        dlg = batch_dialog("批量合成背景音乐")
        check(items_in(dlg).count() == 4, f"弹窗列出了 4 条（实际 {items_in(dlg).count()}）")
        check("视频 2 条" in dlg.inner_text(), "弹窗说明了这一批里有几条视频")
        check("图片/其他 2 条" in dlg.inner_text(), "弹窗说明了这一批里有几条图片/其他")
        check(
            page4.locator(".el-dialog:visible button:has-text('开始合成')").is_enabled(),
            "只有一首音乐时自动选上，直接可以开始合成",
        )

        # 试听：模板里的 `ref` 会被 Vue 自动解包成 DOM 元素，函数若按 ref 用（`.value`）
        # 拿到的是 undefined，表现就是"点了没反应"。这里钉住批量弹窗自己的那个 <audio>。
        page4.click(".el-dialog:visible button:has-text('试听')")
        page4.wait_for_timeout(500)
        check(
            "/music/" in (dlg.locator("audio").first.get_attribute("src") or ""),
            "批量弹窗里也能试听选中的音乐",
        )

        page4.click(".el-dialog:visible button:has-text('开始合成')")
        page4.wait_for_selector(".el-dialog:visible button:has-text('关闭')", timeout=40000)
        page4.wait_for_timeout(600)

        compose_reqs = [i for i, (k, u) in enumerate(log) if k == "req" and u.endswith("/music/compose")]
        compose_resps = [i for i, (k, u) in enumerate(log) if k == "resp" and u.endswith("/music/compose")]
        first_resp = compose_resps[0] if compose_resps else len(log)
        in_flight = sum(1 for i in compose_reqs if i < first_resp)
        check(len(compose_calls) == 2, f"只对 2 条视频发起了合成（图片跳过，实际 {len(compose_calls)}）")
        check(
            1 <= in_flight <= 2,
            f"同时最多 2 条在合成：第一条响应之前发出 {in_flight} 条（放开跑会是 2 条以上）",
        )
        texts = item_texts(dlg)
        check(len([t for t in texts if "已完成" in t]) == 2, f"2 条视频合成完成（{texts}）")
        check(len([t for t in texts if "不是视频" in t]) == 2, "2 条图片标记为「不是视频，无法换音轨」")
        check(
            any("轻音乐" in t for t in texts),
            "成品文件名带上了音乐名（原名-音乐名）",
        )
        page4.keyboard.press("Escape")
        page4.wait_for_timeout(500)

        bgm_col = page4.locator(".material-list .el-table__row td:nth-child(6)").all_inner_texts()
        check("已合成" in bgm_col, f"列表「背景音乐」列跟着变成已合成：{bgm_col[:5]}")

        # ---- 4d) 批量合成中途「中断」 ----
        # 这一段的"慢"来自前端自己的轮询间隔（不是 route 里 sleep），
        # 所以等一会儿再点中断是能点到的：任务确实还停在"合成中"。
        page4.goto(URL, wait_until="networkidle")
        page4.wait_for_selector(".material-list .el-table__row", timeout=15000)
        page4.wait_for_timeout(400)
        keep_running["n"] = 10  # 让每条都停在"合成中"，留出点中断的时间
        before_cancel = len(cancel_calls)

        # 第一页第 3~5 行都是视频（i=22/21/20）：2 条会被 worker 取走，第 3 条还在排队
        open_batch_dialog("批量合成背景音乐", [2, 3, 4])
        page4.click(".el-dialog:visible button:has-text('开始合成')")
        page4.wait_for_selector(".el-dialog:visible .batch-task-item:has-text('进行中')", timeout=8000)
        page4.wait_for_timeout(900)
        page4.click(".el-dialog:visible button:has-text('中断合成')")
        page4.wait_for_selector(".el-dialog:visible button:has-text('关闭')", timeout=40000)
        page4.wait_for_timeout(600)

        texts = item_texts(batch_dialog("批量合成背景音乐"))
        check(
            len([t for t in texts if "已中断" in t]) >= 1,
            f"中断后至少 1 条标记为已中断（{texts}）",
        )
        check(
            len([t for t in texts if "未执行" in t]) >= 1,
            "还没来得及开始的那些标成「已中断，未执行」",
        )
        check(
            not any("进行中" in t for t in texts),
            "中断后没有条目还卡在「进行中」",
        )
        check(
            len(cancel_calls) > before_cancel,
            f"中断时真的去掐了后端的 ffmpeg（cancel 调用 {len(cancel_calls) - before_cancel} 次）",
        )
        page4.keyboard.press("Escape")
        page4.wait_for_timeout(400)

        # ---- 4e) 有一条失败不能把整批停掉，失败原因留在那一条上 ----
        page4.goto(URL, wait_until="networkidle")
        page4.wait_for_selector(".material-list .el-table__row", timeout=15000)
        page4.wait_for_timeout(400)
        fail_next["n"] = 1  # 这一批的第一条会失败
        analyze_before = len(analyze_calls)

        open_batch_dialog("批量生成文案", [0, 1])
        dlg = batch_dialog("批量生成文案")
        # 第 2 行在上一段刚生成过文案，默认会被"跳过已有文案"挡下——
        # 去掉那个勾，它就回到「等待中」，顺便把"改主意"这条路也验了
        dlg.locator(".el-checkbox:has-text('跳过已有 AI 文案')").click()
        page4.wait_for_timeout(300)
        check(
            "等待中" in " ".join(item_texts(dlg)),
            "取消「跳过已有文案」后，已有文案的那条回到等待中",
        )

        page4.click(".el-dialog:visible button:has-text('开始生成')")
        page4.wait_for_selector(".el-dialog:visible button:has-text('关闭')", timeout=30000)
        page4.wait_for_timeout(600)

        texts = item_texts(dlg)
        check(len(analyze_calls) - analyze_before == 2, "失败的那条没有让整批提前结束，后面的照跑")
        check(len([t for t in texts if "失败" in t]) == 1, f"1 条标记为失败（{texts}）")
        check(
            any("模拟生成失败" in t for t in texts),
            f"失败原因（后端返回的 msg）显示在那一条上，不用去翻控制台：{texts}",
        )
        check(len([t for t in texts if "已完成" in t]) == 1, "同批里其他条正常完成")
        page4.keyboard.press("Escape")
        page4.wait_for_timeout(400)

        # ---------------- 5) 真实生成文案（调模型，素材会被改名） ----------------
        if "--with-generate" in sys.argv:
            print("\n[5] 真实点击「生成文案」（调用视觉模型，素材会按标题改名）")
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
            # AI 文案列现在是第 5 列（第 1 列让给了多选框）
            after = page.locator(".material-list .el-table__row td:nth-child(5)").all_inner_texts()
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
