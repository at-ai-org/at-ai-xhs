# -*- coding: utf-8 -*-
"""拼多多发布页选择器核对脚本（真实浏览器 + 真实登录态）。

为什么需要它
------------
拼多多的发布页**必须登录才渲染**，所以写这套接入代码的时候（2026-10）手上没有登录态页面：
`uploader/pdd_uploader/main.py` 里那些选择器是按平台前端产物里的文案先写的一版，
文案（「添加视频」「立即发布」「定时发布」「请添加封面」…）是确定的，
**元素层级不是**。与其猜，不如让用户扫一次码，然后把这个页面扫一遍。

这个脚本做三件事：

1. 带着指定账号的 cookie 打开发布页，回答"登录态还有效吗"（跳到 /login 就是失效）；
2. 把 `PDD_SELECTORS` 里每一组候选选择器逐个试一遍，报告**哪一条命中、命中的是哪个元素**；
3. 把当时的完整 HTML、全页截图、以及所有 input / textarea / button 的清单一并落盘
   （`pdd_debug/probe-<时间戳>/`），这样改选择器不用再让我猜页面长什么样。

用法
----
    # 用「账号管理」里已经加过的账号（按 id 查库里的 cookie 文件）
    .venv/bin/python tools/verify_pdd_publish_page.py --account-id 3

    # 或者直接给 cookie 文件
    .venv/bin/python tools/verify_pdd_publish_page.py --cookie cookiesFile/xxxx.json

    # 想看着它跑（首次接入强烈建议）
    .venv/bin/python tools/verify_pdd_publish_page.py --account-id 3 --headed

退出码：0 = 登录态有效且已生成报告；1 = 打不开页面；2 = 登录态失效。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

# 脚本在 tools/ 下，仓库根要进 sys.path 才能 import conf（它会设置 PLAYWRIGHT_BROWSERS_PATH）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conf import BASE_DIR  # noqa: E402  # 导入它会把 PLAYWRIGHT_BROWSERS_PATH 指到项目内的 .browsers/
from conf import PDD_DEBUG_DIR  # noqa: E402
from conf import PDD_LOGIN_URL_MARKER  # noqa: E402
from conf import PDD_PUBLISH_URL  # noqa: E402
from conf import PDD_PUBLISH_URL_LEGACY  # noqa: E402
from patchright.sync_api import sync_playwright  # noqa: E402
from uploader.pdd_uploader.main import PDD_SELECTORS  # noqa: E402


def _resolve_cookie_file(args) -> Path:
    """把 --cookie / --account-id 解析成一个真实存在的 cookie 文件路径。"""
    if args.cookie:
        path = Path(args.cookie)
        if not path.is_absolute():
            path = Path(BASE_DIR) / path
        if not path.is_file():
            raise SystemExit(f"❌ cookie 文件不存在: {path}")
        return path

    db_path = Path(BASE_DIR) / "db" / "database.db"
    if not db_path.is_file():
        raise SystemExit(f"❌ 找不到账号库: {db_path}（也可以用 --cookie 直接给文件）")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, type, filePath, userName FROM user_info WHERE id = ?",
            (args.account_id,),
        ).fetchone()
    if row is None:
        raise SystemExit(f"❌ 账号库里没有 id={args.account_id} 这个账号")
    if int(row["type"] or 0) != 5:
        print(f"⚠️ 账号 {row['userName']}（id={row['id']}）的 type={row['type']} 不是 5（拼多多视频），继续按拼多多跑")
    path = Path(BASE_DIR) / "cookiesFile" / (row["filePath"] or "")
    if not path.is_file():
        raise SystemExit(f"❌ 账号 {row['userName']} 指向的 cookie 文件不存在: {path}")
    print(f"👤 账号: {row['userName']}（id={row['id']}）→ {path.name}")
    return path


#: 这些选择器组"存在于 DOM"就算命中（文件输入框天生隐藏）。
_HIDDEN_OK_GROUPS = {"file_input"}


def _probe_selectors(page) -> dict:
    """把 PDD_SELECTORS 里每一组候选逐个试一遍，报告命中情况。

    判据**刻意和驱动保持一致**（见 `uploader/pdd_uploader/main.py::_first_visible`）：
    "命中"= 该选择器下存在**可见**的元素，不是"DOM 里匹配到第一个"。
    用 `.first` 去判会把报告写歪——页面上同名按钮一堆，第一个往往是隐藏的那个，
    报告说"没命中"，而驱动其实能找到可见的那个（这个坑离线自测抓过一次）。
    """
    report: dict[str, dict] = {}
    for name, candidates in PDD_SELECTORS.items():
        hits = []
        for selector in candidates:
            entry: dict = {"selector": selector}
            try:
                locator = page.locator(selector)
                count = locator.count()
            except Exception as exc:  # noqa: BLE001
                entry.update({"count": None, "error": str(exc)[:120]})
                hits.append(entry)
                continue
            entry["count"] = count
            entry["hidden_ok"] = name in _HIDDEN_OK_GROUPS
            visible_index = None
            for index in range(min(count, 12)):
                try:
                    if locator.nth(index).is_visible():
                        visible_index = index
                        break
                except Exception:  # noqa: BLE001
                    continue
            entry["visible_index"] = visible_index
            if visible_index is not None:
                try:
                    first = locator.nth(visible_index)
                    entry["tag"] = first.evaluate("el => el.tagName.toLowerCase()")
                    entry["text"] = (first.inner_text() or "")[:80]
                    entry["placeholder"] = first.get_attribute("placeholder") or ""
                    entry["class"] = (first.get_attribute("class") or "")[:120]
                except Exception as exc:  # noqa: BLE001
                    entry["error"] = str(exc)[:120]
            hits.append(entry)
        report[name] = {
            "candidates": hits,
            "matched": next(
                (
                    h["selector"]
                    for h in hits
                    # 文件输入框**天生是隐藏的**（真实页面核对：两个上传 input 都是 visible=False），
                    # 对这一组"存在于 DOM"就算命中；其余组仍然要求可见。
                    if h.get("visible_index") is not None
                    or (h.get("hidden_ok") and h.get("count"))
                ),
                None,
            ),
        }
    return report


def _dom_inventory(page) -> dict:
    """页面上所有 input / textarea / 按钮的清单——选择器全不对时靠它重建。"""
    return page.evaluate(
        """() => {
            const brief = (el) => ({
                tag: el.tagName.toLowerCase(),
                type: el.getAttribute('type') || '',
                accept: el.getAttribute('accept') || '',
                placeholder: el.getAttribute('placeholder') || '',
                className: (el.getAttribute('class') || '').slice(0, 120),
                text: (el.innerText || el.value || '').trim().slice(0, 60),
                visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
            });
            return {
                url: location.href,
                title: document.title,
                inputs: Array.from(document.querySelectorAll('input')).map(brief),
                textareas: Array.from(document.querySelectorAll('textarea')).map(brief),
                editables: Array.from(document.querySelectorAll('[contenteditable="true"]')).map(brief),
                fileInputs: Array.from(document.querySelectorAll('input[type=file]')).map(brief),
                buttons: Array.from(document.querySelectorAll('button, [role=button]')).map(brief).slice(0, 40),
                bodyText: (document.body.innerText || '').slice(0, 3000),
            };
        }"""
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="拼多多视频发布页选择器核对")
    parser.add_argument("--cookie", help="cookiesFile/ 下的文件名或绝对路径")
    parser.add_argument("--account-id", type=int, help="账号管理里的账号 id（自动查 cookie 文件）")
    parser.add_argument("--headed", action="store_true", help="有头模式：直接看页面在干什么")
    parser.add_argument("--legacy-url", action="store_true", help="改用老版发布页地址 /creator/short-video 探测")
    args = parser.parse_args()

    if not args.cookie and not args.account_id:
        parser.error("要么给 --cookie，要么给 --account-id")

    cookie_file = _resolve_cookie_file(args)
    url = PDD_PUBLISH_URL_LEGACY if args.legacy_url else PDD_PUBLISH_URL
    out_dir = Path(BASE_DIR) / PDD_DEBUG_DIR / f"probe-{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"🌐 目标: {url}")
    print(f"📁 报告目录: {out_dir}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed, channel="chromium")
        try:
            context = browser.new_context(storage_state=str(cookie_file))
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(6000)

            if PDD_LOGIN_URL_MARKER in page.url:
                print(f"🥹 登录态已失效：页面被跳到了 {page.url}")
                print("   请到「账号管理」重新扫码登录拼多多视频（达人）后再跑这个脚本。")
                page.screenshot(path=str(out_dir / "login-redirect.png"), full_page=True)
                return 2

            print(f"✅ 停在发布页: {page.url}")

            inventory = _dom_inventory(page)
            selector_report = _probe_selectors(page)

            (out_dir / "page.html").write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(out_dir / "page.png"), full_page=True)
            (out_dir / "report.json").write_text(
                json.dumps({"url": page.url, "selectors": selector_report, "dom": inventory},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            print("\n=== 候选选择器命中情况 ===")
            for name, info in selector_report.items():
                matched = info["matched"] or "❌ 一条都没命中"
                print(f"  {name:14s} → {matched}")
            print("\n=== 页面元素概览 ===")
            print(f"  input: {len(inventory['inputs'])} 个 / textarea: {len(inventory['textareas'])} 个 / "
                  f"可编辑区: {len(inventory['editables'])} 个 / file: {len(inventory['fileInputs'])} 个")
            for item in inventory["fileInputs"]:
                print(f"    file: accept={item['accept']!r} class={item['className'][:60]!r}")
            for item in inventory["inputs"][:12]:
                print(f"    input: type={item['type']!r} placeholder={item['placeholder']!r} "
                      f"visible={item['visible']}")
            for item in inventory["buttons"][:12]:
                print(f"    button: {item['text']!r} visible={item['visible']}")

            print(f"\n📄 报告已写入 {out_dir}/report.json（含完整 HTML / 截图 / 元素清单）")
            return 0
        finally:
            browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
