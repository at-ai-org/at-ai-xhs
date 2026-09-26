# -*- coding: utf-8 -*-
"""小红书页面操作层 —— 无账号冒烟测试。

用途
----
在**没有登录账号**的情况下，验证页面操作层最基础的三件事是否正常：

1. 能不能真的驱动浏览器（patchright 启动、导航）；
2. 能不能正确识别"未登录被重定向"；
3. `selftest()` 的契约探测与报告渲染是否工作。

预期结果
--------
打开 creator.xiaohongshu.com，**应该**检测到登录页——这正是"未登录时行为正确"的证明。
报告里 publish 页的契约会大量缺失，这是预期的（没登录根本到不了发布页）。

用法::

    python examples/xhs_page_ops_smoke.py            # 无头（用 patchright 自带的 chromium）
    python examples/xhs_page_ops_smoke.py --headed   # 有头，肉眼确认
    python examples/xhs_page_ops_smoke.py --channel chrome   # 改用本机 Google Chrome
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import conf  # noqa: E402,F401  （先导入 conf，它会设置浏览器安装目录环境变量）

from patchright.async_api import async_playwright  # noqa: E402

from uploader.xiaohongshu_uploader import selectors as S  # noqa: E402
from uploader.xiaohongshu_uploader.page_ops import XhsPageOps  # noqa: E402


async def run(headless: bool, channel: str) -> int:
    async with async_playwright() as playwright:
        print(f"启动浏览器（channel={channel}, headless={headless}）…")
        browser = await playwright.chromium.launch(headless=headless, channel=channel)
        context = await browser.new_context()
        try:
            page = await context.new_page()
            ops = XhsPageOps(page, screenshot_dir="screenshots")

            r = await ops.goto(S.PUBLISH_ENTRY_PATH)
            print(f"  {r}")
            if r.failed:
                return 2

            await asyncio.sleep(3)
            on_login = await ops.is_login_page()
            print(f"  是否被重定向到登录页: {on_login}  (url={page.url})")

            report = await ops.selftest(pages=("login", "publish"))
            report.url = page.url
            print(report.render())

            login_hits = [i for i in report.items if i.page == "login" and i.found]
            publish_hits = [i for i in report.items if i.page == "publish" and i.found]
            print(f"登录页契约命中 {len(login_hits)} 个 / 发布页契约命中 {len(publish_hits)} 个")
            if on_login and login_hits:
                print("\n✅ 冒烟测试通过：浏览器可驱动、登录态可检测、契约探测可用、报告可渲染")
                return 0
            print("\n⚠️ 冒烟测试未完全通过，请检查上面的输出")
            return 1
        finally:
            await context.close()
            await browser.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="小红书页面操作层冒烟测试（无需登录）")
    ap.add_argument("--headed", action="store_true", help="打开有头浏览器，肉眼确认")
    ap.add_argument("--channel", default="chromium", help="浏览器通道：chromium（默认）或 chrome")
    args = ap.parse_args()
    return asyncio.run(run(headless=not args.headed, channel=args.channel))


if __name__ == "__main__":
    raise SystemExit(main())
