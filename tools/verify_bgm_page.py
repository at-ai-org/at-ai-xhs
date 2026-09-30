# -*- coding: utf-8 -*-
"""背景音乐页 + 「合成背景音乐」全链路验收（Playwright 驱动真实浏览器）。

**和 tools/ 下另外两个脚本最大的区别：这个脚本自己起一套隔离环境。**
它不需要你先 `./start.sh`，也不会碰你真实的 `videoFile/`、`bgmFile/`、
`database.db` 和 `settings.json`：

    临时目录/
      conf.py          ← 真仓库那份的**副本**（BASE_DIR 因此落在沙箱里）
      settings.json    ← 现造的账号密码
      db/ videoFile/ bgmFile/ logs/   ← 现造的空目录 + 空库
      其余（sau_backend*.py、myUtils/、utils/、uploader/…）← 软链到真仓库

软链的是**代码本体**，所以改完代码直接重跑即可，不用同步任何东西；
真实数据一个字节都不会动。后端跑在随机空闲端口上，前端用 `sau_frontend/dist`
的构建产物（同源托管，和 Docker 里的形态一致）——所以先跑一次 `npm run build`。

验收项（都是需求里那几条）：
  1. 左侧菜单出现「背景音乐」，能进页面；
  2. 上传自己的音乐 → 出现在列表里（名称/时长/大小）；「试听」能给 `<audio>` 拼出带 token 的地址；
  3. 素材管理：**UUID 列没有了**，「背景音乐」列排在「AI 文案」之后，新素材默认「待合成」；
  4. 点操作栏的「背景音乐」→ 弹窗里能选到刚上传的音乐 → 合成期间显示进度条、状态列变「合成中」；
  5. 合成完成：弹窗自动关、状态列变「已合成」、文件名变成 `原名-音乐名.mp4`、原文件消失；
  6. 成品真的是"换了音轨"的视频（有视频轨 + 有音频轨 + 时长跟原视频一致）。

用法：
    .venv/bin/python tools/verify_bgm_page.py
    .venv/bin/python tools/verify_bgm_page.py --keep      # 留住沙箱，方便事后翻日志
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from conf import BASE_DIR  # noqa: F401,E402  # 导入它会把 PLAYWRIGHT_BROWSERS_PATH 指到项目内的 .browsers/
from playwright.sync_api import sync_playwright  # noqa: E402

SHOT_DIR = ROOT / "screenshots"
SHOT_DIR.mkdir(exist_ok=True)

VIEWPORT = {"width": 1680, "height": 1000}

#: 沙箱里现造的登录账号密码（和真实 settings.json 无关）
SANDBOX_USER = "admin"
SANDBOX_PASSWORD = "bgm-verify-123456"

#: 测试素材/音乐的名字。刻意用 ASCII 文件名：`_safe_upload_name` 对纯中文名会退化成
#: `video_时间戳.mp4`，那样断言里就没法用固定名字了。
MATERIAL_STEM = "bgm-verify"
MATERIAL_FILE = f"{MATERIAL_STEM}.mp4"
MUSIC_NAME = "轻音乐A"
EXPECTED_OUTPUT = f"{MATERIAL_STEM}-{MUSIC_NAME}.mp4"

#: 测试视频时长（秒）。要足够长才来得及看到「合成中」和进度条：
#: 换音轨虽然是 `-c:v copy`，但音频要整条重编码，时长越长这一步越明显。
VIDEO_SECONDS = 90

failures: list[str] = []


def check(cond, msg):
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        failures.append(msg)


# --------------------------------------------------------------------------- #
# 沙箱
# --------------------------------------------------------------------------- #


def build_sandbox(root: Path) -> Path:
    """搭一套"长得像仓库根"的目录（见模块开头）。"""
    for name in ("db", "videoFile", "bgmFile", "logs", "cookies", "cookiesFile"):
        (root / name).mkdir(parents=True, exist_ok=True)

    shutil.copy(ROOT / "conf.py", root / "conf.py")
    (root / "settings.json").write_text(
        json.dumps({"auth": {"username": SANDBOX_USER, "password": SANDBOX_PASSWORD}}),
        encoding="utf-8",
    )

    for name in (
        "sau_backend.py",
        "sau_backend_xhs.py",
        "sau_backend_music.py",
        "sau_backend_publish.py",
        "sau_cli.py",
        "myUtils",
        "utils",
        "uploader",
    ):
        source = ROOT / name
        if not source.exists():
            raise SystemExit(f"沙箱缺少 {source}：这个脚本要跑在仓库根下（tools/verify_bgm_page.py）")
        os.symlink(source, root / name)

    # 前端产物：同源托管，和容器里的形态一致
    dist = ROOT / "sau_frontend" / "dist"
    if not (dist / "index.html").is_file():
        raise SystemExit(f"找不到 {dist}/index.html：先 cd sau_frontend && npm run build")
    shutil.copy(dist / "index.html", root / "index.html")
    shutil.copytree(dist / "assets", root / "assets")
    # 标签页图标：index.html 引的是 /logo.svg，后端从 assets/ 里读（和 Dockerfile 同样的做法）
    logo = dist / "logo.svg"
    if logo.is_file():
        shutil.copy(logo, root / "assets" / "logo.svg")

    # 本地构建的产物里写死了 `http://localhost:5409`（.env.production 的值），沙箱换了端口
    # 就必须改掉——否则所有请求会打到**你此刻正在跑的那个真后端**上
    # （等于拿沙箱密码去登真后台）。做法和 Dockerfile 里那段一样：置空 → 全部走同源。
    for bundle in (root / "assets").glob("*.js"):
        text = bundle.read_text(encoding="utf-8")
        bundle.write_text(text.replace("http://localhost:5409", ""), encoding="utf-8")
    leaked = [
        p.name
        for p in (root / "assets").glob("*.js")
        if "localhost:5409" in p.read_text(encoding="utf-8")
    ]
    if leaked:
        raise SystemExit(f"前端产物里还有写死的 API 地址，沙箱会误连真后端：{leaked}")

    # 建表脚本按 Dockerfile 的做法另存一份，再 cd 到 db/ 里跑（它写死了 './database.db'）
    (root / "scripts").mkdir(exist_ok=True)
    shutil.copy(ROOT / "db" / "createTable.py", root / "scripts" / "createTable.py")
    subprocess.run(
        [sys.executable, str(root / "scripts" / "createTable.py")],
        cwd=root / "db",
        check=True,
        capture_output=True,
    )

    # sa_backend.py 里端口是写死的 5409，这里用一层壳换端口，免得和你在跑的那个后端撞车
    port = free_port()
    (root / "serve.py").write_text(
        "import sau_backend\n"
        f"sau_backend.app.run(host='127.0.0.1', port={port})\n",
        encoding="utf-8",
    )
    return port


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_test_media(root: Path):
    """现造一条测试素材和一首测试音乐（都用 ffmpeg，不依赖任何现成文件）。"""
    from myUtils.background_music import find_ffmpeg

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise SystemExit("找不到 ffmpeg（requirements.txt 里的 imageio-ffmpeg 自带）")

    video = root / "bench" / MATERIAL_FILE
    music = root / "bench" / f"{MUSIC_NAME}.mp3"
    video.parent.mkdir(exist_ok=True)

    def run(args):
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True
        )
        if proc.returncode != 0:
            raise SystemExit(proc.stderr.decode("utf-8", "ignore"))

    # 时长要长、体积要小：10fps + ultrafast + 高 crf。这样"换音轨"那一步（音频要重编码）
    # 有足够耗时，界面上才看得到「合成中」和进度条。
    run([
        "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=10:duration={VIDEO_SECONDS}",
        "-f", "lavfi", "-i", f"sine=frequency=200:duration={VIDEO_SECONDS}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "38", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(video),
    ])
    run(["-f", "lavfi", "-i", "sine=frequency=2000:duration=4", "-c:a", "libmp3lame", str(music)])
    return video, music


def wait_for_server(base: str, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/auth/status", timeout=3) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    raise SystemExit(f"后端在 {timeout:.0f}s 内没起来：{base}")


# --------------------------------------------------------------------------- #
# 浏览器交互
# --------------------------------------------------------------------------- #


def login(page, base: str):
    page.goto(f"{base}/#/login", wait_until="networkidle")
    page.fill('input[autocomplete="username"]', SANDBOX_USER)
    page.fill('input[autocomplete="current-password"]', SANDBOX_PASSWORD)
    page.click(".login-button")
    page.wait_for_selector(".sidebar-menu", timeout=15000)


def row_by_text(page, text: str, table: str):
    return page.locator(f"{table} .el-table__row").filter(has_text=text)


def cell_texts(row):
    return [c.strip() for c in row.locator("td").all_inner_texts()]


def upload_through_dialog(page, open_button: str, dialog_title: str, files, name_field=None, name_value=None):
    page.click(f"button:has-text('{open_button}')")
    dialog = page.locator(f".el-dialog:has-text('{dialog_title}')")
    dialog.wait_for(state="visible", timeout=10000)
    dialog.locator('input[type="file"]').set_input_files([str(f) for f in files])
    page.wait_for_timeout(300)
    if name_field and name_value:
        dialog.locator(f'.el-form-item:has-text("{name_field}") input').first.fill(name_value)
    dialog.locator('button:has-text("确认上传")').click()
    # 全部成功时上传窗口会自动关掉
    dialog.wait_for(state="hidden", timeout=120000)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留沙箱目录（排障用）")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="bgm-verify-"))
    print(f"沙箱目录：{tmp}")
    server = None
    try:
        port = build_sandbox(tmp)
        base = f"http://127.0.0.1:{port}"
        print(f"启动隔离后端：{base}")

        video, music = make_test_media(tmp)

        server = subprocess.Popen(
            [sys.executable, str(tmp / "serve.py")],
            cwd=tmp,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        wait_for_server(base)

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport=VIEWPORT)
            errors: list[str] = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))

            print("\n[1] 登录 + 左侧菜单里的「背景音乐」")
            login(page, base)
            menu = page.locator('.sidebar-menu .el-menu-item:has-text("背景音乐")')
            check(menu.count() == 1, "左侧菜单出现「背景音乐」")
            menu.click()
            page.wait_for_selector(".background-music", timeout=10000)
            check("/background-music" in page.url, f"进入背景音乐页（{page.url.split('#')[-1]}）")

            print("\n[2] 上传音乐 + 列表 + 试听")
            upload_through_dialog(
                page, "上传音乐", "上传背景音乐", [music], name_field="音乐名称", name_value=MUSIC_NAME
            )
            page.wait_for_selector(f".music-container .el-table__row:has-text('{MUSIC_NAME}')", timeout=15000)
            row = row_by_text(page, MUSIC_NAME, ".music-container")
            cells = cell_texts(row)
            check(MUSIC_NAME in cells[0], f"音乐出现在列表里：{cells[0]}")
            check(cells[1] not in ("", "—"), f"时长已探测到：{cells[1]}")
            check("MB" in cells[2], f"文件大小已记录：{cells[2]}")
            page.screenshot(path=str(SHOT_DIR / "bgm_music_page.png"), full_page=True)

            row.locator('button:has-text("试听")').click()
            page.wait_for_timeout(800)
            src = page.locator("audio").first.get_attribute("src") or ""
            check("/music/" in src and "token=" in src, "试听会带上登录态（<audio> 只能靠查询串带 token）")
            row.locator('button:has-text("停止")').click()

            print("\n[3] 素材管理：列结构 + 默认「待合成」")
            page.goto(f"{base}/#/material-management", wait_until="networkidle")
            upload_through_dialog(page, "上传素材", "上传素材", [video])
            page.wait_for_selector(f".material-list .el-table__row:has-text('{MATERIAL_STEM}')", timeout=30000)

            headers = [h.strip() for h in page.locator(".material-list .el-table__header th").all_inner_texts()]
            check("UUID" not in headers, f"UUID 列已去掉（当前列：{' / '.join(headers)}）")
            check(
                "背景音乐" in headers
                and headers.index("背景音乐") == headers.index("AI 文案") + 1,
                "「背景音乐」列排在「AI 文案」之后",
            )

            material_row = row_by_text(page, MATERIAL_STEM, ".material-list")
            cells = cell_texts(material_row)
            check(cells[headers.index("背景音乐")] == "待合成", f"新素材默认「待合成」（读到 {cells[headers.index('背景音乐')]}）")
            page.screenshot(path=str(SHOT_DIR / "bgm_material_list.png"), full_page=True)

            print("\n[4] 合成：选音乐 → 进度 → 状态列")
            # 按钮上的字是「背景音乐」（操作列里的那个），弹窗标题才是「合成背景音乐」
            material_row.locator('button:has-text("背景音乐")').click()
            dialog = page.locator(".el-dialog:has-text('合成背景音乐')")
            dialog.wait_for(state="visible", timeout=10000)
            check(dialog.locator(".bgm-material").count() == 1, "弹窗里显示了当前素材")

            dialog.locator(".bgm-select").click()
            option = page.locator(f'.el-select-dropdown__item:has-text("{MUSIC_NAME}")').first
            option.wait_for(state="visible", timeout=8000)
            option.click()
            page.wait_for_timeout(200)
            dialog.locator('button:has-text("试听")').click()
            page.wait_for_timeout(600)
            dialog_src = page.locator("audio").first.get_attribute("src") or ""
            check(f"/music/" in dialog_src, "弹窗里能试听选中的音乐")

            dialog.locator('button:has-text("开始合成")').click()
            # 进度条是任务一提交就出现的（不等 ffmpeg 的第一行输出）
            dialog.locator(".bgm-progress").wait_for(state="visible", timeout=10000)
            check(True, "合成期间显示进度条")

            # 状态列变「合成中」：前端在发请求前就乐观置位，所以这里一定赶得上
            saw_running = False
            deadline = time.time() + 20
            while time.time() < deadline and not saw_running:
                cells = cell_texts(row_by_text(page, MATERIAL_STEM, ".material-list"))
                saw_running = "合成中" in cells
                time.sleep(0.05)
            check(saw_running, "状态列显示「合成中」")
            page.screenshot(path=str(SHOT_DIR / "bgm_composing.png"))

            print("\n[5] 合成完成：改名 + 状态 + 原文件消失")
            dialog.wait_for(state="hidden", timeout=180000)
            page.wait_for_selector(
                f".material-list .el-table__row:has-text('{EXPECTED_OUTPUT}')", timeout=30000
            )
            check(
                row_by_text(page, MATERIAL_FILE, ".material-list").count() == 0,
                f"原文件那一行没了（已被 {EXPECTED_OUTPUT} 取代）",
            )
            done_row = row_by_text(page, EXPECTED_OUTPUT, ".material-list")
            cells = cell_texts(done_row)
            check(EXPECTED_OUTPUT in cells[headers.index("文件名")], f"文件名带上了音乐名：{cells[headers.index('文件名')]}")
            check(cells[headers.index("背景音乐")] == "已合成", f"状态列变「已合成」（读到 {cells[headers.index('背景音乐')]}）")
            page.screenshot(path=str(SHOT_DIR / "bgm_composed.png"), full_page=True)

            print("\n[6] 成品本身：真的换了音轨")
            from myUtils.background_music import probe_media

            out_path = tmp / "videoFile" / EXPECTED_OUTPUT
            info = probe_media(out_path)
            check(out_path.is_file(), f"成品落在沙箱的 videoFile/ 下：{out_path.name}")
            check(info["has_video"] and info["has_audio"], f"成品同时有画面和声音：{info}")
            check(
                abs((info["duration"] or 0) - VIDEO_SECONDS) < 2,
                f"时长跟着原视频（{info['duration']:.1f}s ≈ {VIDEO_SECONDS}s，4 秒的音乐被循环铺满）",
            )
            check(not (tmp / "videoFile" / MATERIAL_FILE).exists(), "原素材文件已删除（原地替换）")

            print("\n[7] 控制台无报错")
            check(not errors, f"浏览器控制台干净（{'；'.join(errors[:3]) if errors else '无 error'}）")

            browser.close()
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
        if args.keep:
            print(f"\n（--keep）沙箱保留在：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    if failures:
        print(f"❌ {len(failures)} 项未通过：")
        for item in failures:
            print(f"   · {item}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
