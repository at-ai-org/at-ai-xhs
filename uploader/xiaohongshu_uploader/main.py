# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import inspect
import os
from datetime import datetime
from pathlib import Path

from patchright.async_api import Page
from patchright.async_api import Playwright
from patchright.async_api import async_playwright

from conf import DEBUG_MODE, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from uploader.base_video import BaseVideoUploader
from utils.base_social_media import set_init_script
from utils.login_qrcode import build_login_qrcode_path
from utils.login_qrcode import decode_qrcode_from_path
from utils.login_qrcode import print_terminal_qrcode
from utils.login_qrcode import remove_qrcode_file
from utils.login_qrcode import save_data_url_image
from utils.log import xiaohongshu_logger

from . import selectors as S
from .page_ops import OpResult
from .page_ops import SelftestReport
from .page_ops import XhsPageOps

XHS_DEFAULT_CREATOR_BASE_URL = "https://creator.xiaohongshu.com"
XHS_CREATOR_BASE_URL_ENV = "SAU_XHS_CREATOR_BASE_URL"
XHS_PUBLISH_SUCCESS_URL_PATTERN = "**/publish/success?**"
XHS_LOGIN_BOX_SELECTOR = "div[class*='login-box']"
XHS_LOGIN_SWITCH_SELECTOR = "img.css-wemwzq"
XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED = "scheduled"


def _build_xhs_creator_url(path: str) -> str:
    base_url = os.getenv(
        XHS_CREATOR_BASE_URL_ENV,
        XHS_DEFAULT_CREATOR_BASE_URL,
    ).strip().rstrip("/")
    if not base_url:
        base_url = XHS_DEFAULT_CREATOR_BASE_URL
    return f"{base_url}/{path.lstrip('/')}"


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


async def _js_click_by_text(page: Page, text: str) -> bool:
    """用 JS 找到文字完全匹配的最内层元素并点击它及其祖先（绕过 span pointer-events:none / 遮罩拦截）。

    小红书很多可点项文字在 <span class="d-text"> 里，pointer-events 常被禁用，
    Playwright 常规 click 会超时。用原生 click 冒泡触发 Vue 事件更可靠。
    """
    return await page.evaluate(
        """(t) => {
            const nodes = [...document.querySelectorAll('*')].filter(
                e => e.children.length === 0 && (e.textContent || '').trim() === t
            );
            if (!nodes.length) return false;
            let el = nodes[nodes.length - 1];
            for (let i = 0; i < 4 && el; i++) { try { el.click(); } catch (e) {} el = el.parentElement; }
            return true;
        }""",
        text,
    )


async def _emit_qrcode_callback(qrcode_callback, payload: dict):
    if not qrcode_callback:
        return

    callback_result = qrcode_callback(payload)
    if inspect.isawaitable(callback_result):
        await callback_result


def _build_login_result(
    success: bool,
    status: str,
    message: str,
    account_file: str,
    qrcode: dict | None = None,
    current_url: str = "",
) -> dict:
    return {
        "success": success,
        "status": status,
        "message": message,
        "account_file": str(account_file),
        "qrcode": qrcode,
        "current_url": current_url,
    }


async def _open_xhs_qrcode_panel(page: Page) -> None:
    login_box = page.locator(XHS_LOGIN_BOX_SELECTOR).first
    await login_box.wait_for(state="visible", timeout=30000)

    scan_text = login_box.locator("div:has-text('扫一扫')").first
    if await scan_text.count():
        return

    switch_img = login_box.locator(XHS_LOGIN_SWITCH_SELECTOR).first
    await switch_img.wait_for(state="visible", timeout=10000)
    await switch_img.click()
    await login_box.locator("div:has-text('扫一扫')").first.wait_for(state="visible", timeout=10000)


async def _find_xhs_qrcode_locator(page: Page):
    await _open_xhs_qrcode_panel(page)

    qrcode_img = page.locator('.login-box-container').get_by_text("APP扫一扫登录").filter(visible=True).locator("xpath=..//following-sibling::div//img").nth(0)

    if await qrcode_img.count():
        return qrcode_img

    raise RuntimeError("未在扫一扫登录区域找到小红书二维码图片")


async def _extract_xhs_qrcode_src(page: Page) -> str:
    qrcode_img = await _find_xhs_qrcode_locator(page)
    await qrcode_img.wait_for(state="visible", timeout=30000)
    qrcode_src = await qrcode_img.get_attribute("src")
    if not qrcode_src:
        raise RuntimeError("未获取到小红书登录二维码地址")
    return qrcode_src


async def _save_xhs_qrcode(
    page: Page,
    account_file: str,
    previous_qrcode_path: Path | None = None,
    qrcode_callback=None,
) -> dict:
    qrcode_src = await _extract_xhs_qrcode_src(page)
    qrcode_path = build_login_qrcode_path(account_file, suffix="xhs_login_qrcode")
    qrcode_img = await _find_xhs_qrcode_locator(page)

    if qrcode_src.startswith("data:image/"):
        save_data_url_image(qrcode_src, qrcode_path)
    else:
        qrcode_path.parent.mkdir(parents=True, exist_ok=True)
        await qrcode_img.screenshot(path=str(qrcode_path))

    if previous_qrcode_path and previous_qrcode_path != qrcode_path:
        if remove_qrcode_file(previous_qrcode_path):
            xiaohongshu_logger.info(_msg("🧹", f"临时二维码文件已清理: {previous_qrcode_path}"))

    xiaohongshu_logger.info(_msg("🖼️", f"二维码已经准备好啦，已保存到: {qrcode_path}"))
    qrcode_content = decode_qrcode_from_path(qrcode_path)
    if qrcode_content:
        print_terminal_qrcode(qrcode_content, qrcode_path, "小红书APP")
    else:
        xiaohongshu_logger.warning(_msg("😵", f"终端没法完整显示二维码，请打开 {qrcode_path} 扫码"))

    qrcode_info = {
        "image_path": str(qrcode_path),
        "image_data_url": qrcode_src,
    }
    await _emit_qrcode_callback(qrcode_callback, qrcode_info)
    return qrcode_info


async def _is_xhs_login_completed(page: Page) -> bool:
    if page.url.startswith(_build_xhs_creator_url("/login")):
        return False

    login_box = page.locator(XHS_LOGIN_BOX_SELECTOR).first
    if not await login_box.count():
        return True

    try:
        return not await login_box.is_visible()
    except Exception:
        return True


async def cookie_auth(account_file):
    if not os.path.exists(account_file):
        return False

    async with async_playwright() as playwright:
        if LOCAL_CHROME_PATH:
            browser = await playwright.chromium.launch(headless=True, executable_path=LOCAL_CHROME_PATH)
        else:
            browser = await playwright.chromium.launch(headless=True, channel="chromium")
        try:
            context = await browser.new_context(storage_state=account_file)
            context = await set_init_script(context)
            page = await context.new_page()
            await page.goto(
                _build_xhs_creator_url(
                    "/publish/publish?from=homepage&target=video"
                )
            )
            await page.wait_for_timeout(3000)

            if page.url.startswith(_build_xhs_creator_url("/login")):
                xiaohongshu_logger.info(_msg("🥹", "cookie 已失效，得重新登录一下"))
                return False

            login_box = page.locator(XHS_LOGIN_BOX_SELECTOR).first
            if await login_box.count():
                try:
                    if await login_box.is_visible():
                        xiaohongshu_logger.info(_msg("🥹", "页面仍然停留在登录二维码页，按 cookie 失效处理"))
                        return False
                except Exception:
                    return False

            xiaohongshu_logger.success(_msg("🥳", "cookie 有效"))
            return True
        except Exception as exc:
            xiaohongshu_logger.warning(_msg("😵", f"cookie 校验时出错，按失效处理: {exc}"))
            return False
        finally:
            await browser.close()


async def xiaohongshu_setup(
    account_file,
    handle=False,
    return_detail=False,
    qrcode_callback=None,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            result = _build_login_result(False, "cookie_invalid", "cookie文件不存在或已失效", account_file)
            return result if return_detail else False
        xiaohongshu_logger.info(_msg("🥹", "cookie 失效了，准备打开浏览器重新登录"))
        result = await xiaohongshu_cookie_gen(
            account_file,
            qrcode_callback=qrcode_callback,
            headless=headless,
        )
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie有效", account_file)
    return result if return_detail else True


async def xiaohongshu_cookie_gen(
    account_file,
    qrcode_callback=None,
    poll_interval: int = 3,
    max_checks: int = 100,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    if headless:
        xiaohongshu_logger.info(_msg("🖼️", "小红书登录将以无头模式运行，小人会输出终端二维码并保存本地二维码图片"))

    account_path = Path(account_file)
    account_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless, channel="chromium")
        context = await browser.new_context()
        context = await set_init_script(context)
        qrcode_path = None
        qrcode_info = None
        result = _build_login_result(False, "failed", "小红书登录失败", account_file)
        try:
            page = await context.new_page()
            await page.goto(_build_xhs_creator_url("/login"))
            qrcode_info = await _save_xhs_qrcode(page, account_file, qrcode_callback=qrcode_callback)
            qrcode_path = Path(qrcode_info["image_path"])
            xiaohongshu_logger.info(_msg("🧍", "请扫码，小人正在耐心等待登录完成"))

            for _ in range(max_checks):
                if await _is_xhs_login_completed(page):
                    await asyncio.sleep(2)
                    await context.storage_state(path=account_file)
                    if await cookie_auth(account_file):
                        xiaohongshu_logger.success(_msg("🥳", "小红书扫码登录成功，小人开心收工"))
                        result = _build_login_result(True, "success", "小红书扫码登录成功", account_file, qrcode_info, page.url)
                    else:
                        result = _build_login_result(
                            False,
                            "cookie_invalid",
                            "小红书扫码流程结束，但 cookie 校验失败",
                            account_file,
                            qrcode_info,
                            page.url,
                        )
                    return result

                await asyncio.sleep(poll_interval)

            result = _build_login_result(
                False,
                "timeout",
                "等待小红书扫码登录超时",
                account_file,
                qrcode_info,
                page.url,
            )
        except Exception as exc:
            result = _build_login_result(False, "failed", str(exc), account_file, current_url=page.url if "page" in locals() else "")
        finally:
            if remove_qrcode_file(qrcode_path):
                xiaohongshu_logger.info(_msg("🧹", f"临时二维码文件已清理: {qrcode_path}"))
            if not result["success"]:
                xiaohongshu_logger.error(_msg("😢", f"登录失败: {result['message']}"))
            await context.close()
            await browser.close()
        return result


class XiaoHongShuBaseUploader(BaseVideoUploader):
    def __init__(
        self,
        publish_date: datetime | int,
        account_file,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        self.publish_date = publish_date
        self.account_file = str(account_file)
        self.publish_strategy = publish_strategy
        self.debug = debug
        self.date_format = "%Y年%m月%d日 %H:%M"
        self.local_executable_path = LOCAL_CHROME_PATH
        self.headless = headless

    async def validate_base_args(self):
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成小红书登录: {self.account_file}")
        if not await cookie_auth(self.account_file):
            raise RuntimeError(f"cookie文件已失效，请先完成小红书登录: {self.account_file}")

        if self.publish_strategy not in {
            XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
            XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED,
        }:
            raise ValueError(f"不支持的发布策略: {self.publish_strategy}")

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED:
            self.publish_date = self.validate_publish_date(self.publish_date)
        else:
            self.publish_date = 0

    async def set_schedule_time_xiaohongshu(self, page: Page, publish_date: datetime):
        xiaohongshu_logger.info(_msg("🕒", f"小人准备设置定时发布时间: {publish_date.strftime(self.date_format)}"))
        await page.locator('.custom-switch-card').filter(has_text="定时发布").locator('.d-switch').click()
        await asyncio.sleep(1)
        publish_date_hour = publish_date.strftime("%Y-%m-%d %H:%M")
        time_input = page.locator('.d-datepicker-input-filter input.d-text')
        await time_input.fill(str(publish_date_hour))
        await asyncio.sleep(1)

    async def set_location(self, page: Page, location: str = "青岛市"):
        if not location:
            return True

        xiaohongshu_logger.info(_msg("📍", f"小人准备设置位置: {location}"))
        loc_ele = await page.wait_for_selector('div.d-text.d-select-placeholder.d-text-ellipsis.d-text-nowrap')
        await loc_ele.click()
        await page.wait_for_timeout(1000)
        await page.keyboard.type(location)
        dropdown_selector = 'div.d-popover.d-popover-default.d-dropdown.--size-min-width-large'
        await page.wait_for_timeout(2000)
        try:
            await page.wait_for_selector(dropdown_selector, timeout=3000)
        except Exception:
            xiaohongshu_logger.warning(_msg("😵", "位置下拉列表没按预期出现，小人继续按旧逻辑查找"))
        await page.wait_for_timeout(1000)
        flexible_xpath = (
            f'//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
            f'//div[contains(@class, "d-options-wrapper")]'
            f'//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
            f'//div[contains(@class, "name") and text()="{location}"]'
        )
        await page.wait_for_timeout(3000)
        try:
            location_option = await page.wait_for_selector(
                flexible_xpath,
                timeout=3000
            )

            if not location_option:
                location_option = await page.wait_for_selector(
                    f'//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
                    f'//div[contains(@class, "d-options-wrapper")]'
                    f'//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
                    f'/div[1]//div[contains(@class, "name") and text()="{location}"]',
                    timeout=2000
                )

            await location_option.scroll_into_view_if_needed()
            await location_option.click()
            xiaohongshu_logger.success(_msg("🥳", f"位置已经设置成 {location}"))
            return True
        except Exception as e:
            xiaohongshu_logger.error(_msg("😢", f"设置位置失败: {e}"))
            try:
                all_options = await page.query_selector_all(
                    '//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
                    '//div[contains(@class, "d-options-wrapper")]'
                    '//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
                    '/div'
                )
                xiaohongshu_logger.debug(_msg("🧍", f"位置下拉里一共找到 {len(all_options)} 个选项"))
                for i, option in enumerate(all_options[:3]):
                    option_text = await option.inner_text()
                    xiaohongshu_logger.debug(_msg("🧾", f"候选位置 {i + 1}: {option_text.strip()[:50]}"))
            except Exception as inner_e:
                xiaohongshu_logger.debug(_msg("😵", f"读取位置候选列表失败: {inner_e}"))
            return False

    async def fill_title(self, page: Page) -> None:
        title_container = page.locator('input[placeholder*="填写标题"]')
        await title_container.fill(self.title[:20])

    async def fill_desc(self, page: Page) -> None:
        if not getattr(self, "desc", ""):
            return

        desc = page.locator('p[data-placeholder*="输入正文描述"]')
        await desc.click()
        await page.keyboard.press("Backspace")
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.press("Delete")
        await page.keyboard.type(self.desc)
        await page.keyboard.press("Enter")

    async def fill_tags(self, page: Page) -> None:
        if not getattr(self, "tags", None):
            return

        # 小红书标签上限为 10 个，超过会导致死循环卡住发布
        max_tags = 10
        if len(self.tags) > max_tags:
            xiaohongshu_logger.warning(
                _msg("🏷️", f"标签数量 {len(self.tags)} 超过小红书上限 {max_tags}，只取前 {max_tags} 个: {self.tags[:max_tags]}")
            )
            self.tags = self.tags[:max_tags]

        if not getattr(self, "desc", ""):
            desc = page.locator('p[data-placeholder*="输入正文描述"]')
            await desc.click()

        for tag in self.tags:  # 循环处理所有 tags
            # 话题候选下拉框依赖小红书联想接口实时返回，网络抖动/无匹配时会等不到。
            # 标签是可选增强项：等不到候选框就跳过该标签继续，不让整条发布因此失败。
            try:
                await page.keyboard.type("#" + tag, delay=30)
                await page.locator('#creator-editor-topic-container').wait_for(
                    state="visible",
                    timeout=6000
                )
                first_item = page.locator('#creator-editor-topic-container .item').first
                await first_item.wait_for(state="visible", timeout=4000)
                await first_item.click()
            except Exception as exc:
                xiaohongshu_logger.warning(
                    _msg("🏷️", f"话题『{tag}』未出现候选，跳过该标签继续发布: {exc}")
                )
                # 清掉已键入但未成词的 "#tag" 文本，避免它残留进正文
                for _ in range(len("#" + tag)):
                    await page.keyboard.press("Backspace")
                continue

    async def fill_meta(self, page: Page) -> None:
        await self.fill_title(page)
        await self.fill_desc(page)
        await self.fill_tags(page)

    async def check_original_declaration(self, page: Page) -> None:
        """设置「来源转载」声明，填写转载来源。

        流程（对应 codegen 录制）：
          点「添加内容类型声明」→ 点包含「来源转载」的 div
          → 填 placeholder「请输入媒体名称」→ 点 button「确认」。
        容错：任一步失败记 warning 跳过、继续发布，不中断。
        """
        source = getattr(self, "repost_source", "") or ""
        try:
            # 1. 点「添加内容类型声明」
            trigger = page.get_by_text("添加内容类型声明", exact=False).first
            try:
                await trigger.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            await trigger.click(force=True)
            await page.wait_for_timeout(1500)

            # 2. 选「来源转载」选项
            import re as _re
            repost_option = page.locator("#publish-container div").filter(
                has_text=_re.compile(r"^来源转载$")
            ).last
            if await repost_option.count():
                await repost_option.click(force=True)
            else:
                await _js_click_by_text(page, "来源转载")
            await page.wait_for_timeout(1500)

            # 3. 填写媒体名称
            source_input = page.get_by_placeholder("请输入媒体名称").first
            await source_input.wait_for(state="visible", timeout=8000)
            await source_input.click()
            await source_input.fill(source)
            await page.wait_for_timeout(500)

            # 4. 点「确认」按钮
            confirm = page.get_by_role("button", name="确认").first
            try:
                await confirm.wait_for(state="visible", timeout=5000)
                await confirm.click()
            except Exception:
                await _js_click_by_text(page, "确认")

            await page.wait_for_timeout(1000)
            xiaohongshu_logger.success(_msg("🧾", f"来源转载已声明（来源：{source}）"))
        except Exception as exc:
            xiaohongshu_logger.warning(_msg("⚠️", f"设置来源转载失败，跳过继续发布: {exc}"))
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass


class XiaoHongShuVideo(XiaoHongShuBaseUploader):
    def __init__(
        self,
        title,
        file_path,
        tags,
        publish_date: datetime | int,
        account_file,
        thumbnail_path=None,
        desc: str | None = None,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        # ---- at-ai-xhs 改造新增 ----
        collection_name: str | None = None,
        visibility: str | None = None,
        is_original: bool = False,
        dry_run: bool = False,
        upload_timeout: float = 600.0,
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
        )
        self.title = title
        self.file_path = file_path
        self.tags = tags or []
        self.thumbnail_path = thumbnail_path
        self.desc = desc or ""
        # 新增：合集 / 可见范围 / 原创声明 / 演练模式
        self.collection_name = collection_name
        self.visibility = visibility
        self.is_original = is_original
        self.dry_run = dry_run
        self.upload_timeout = upload_timeout

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("视频模式下，title 是必须的")

        self.file_path = str(self.validate_video_file(self.file_path))
        if self.thumbnail_path:
            self.thumbnail_path = str(self.validate_image_file(self.thumbnail_path))

    async def handle_upload_error(self, page: Page):
        xiaohongshu_logger.warning(_msg("😵", "视频上传摔了一跤，小人马上重新上传"))
        await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(self.file_path)

    async def set_thumbnail(self, page: Page, thumbnail_path: str):
        if not thumbnail_path:
            return

        xiaohongshu_logger.info(_msg("🖼️", "小人准备设置封面"))

        # 封面设置为增强步骤：失败时记 warning 跳过、继续发布（用视频首帧兜底）。
        try:
            # 发布页封面区域内嵌，点击 div.upload-cover 打开封面弹窗（d-modal）。
            cover_section = page.locator("text=设置封面").first
            try:
                await cover_section.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)

            # 1. 点击 div.upload-cover 打开封面弹窗
            upload_cover = page.locator("div.upload-cover").first
            if not await upload_cover.count():
                upload_cover = page.locator("div.cover-plugin-preview div.default.pointer").first
            await upload_cover.click(force=True)
            await page.wait_for_timeout(3000)

            # 2. 切换到「上传封面」tab（默认在「截取封面」）
            upload_tab = page.get_by_text("上传封面", exact=True).first
            await upload_tab.wait_for(state="visible", timeout=10000)
            await upload_tab.click()
            await page.wait_for_timeout(2000)

            # 3. 找到图片 file input（parent class: upload-wrapper）并上传
            file_input = page.locator('div.upload-wrapper input[type="file"][accept*="image"]').first
            if not await file_input.count():
                file_input = page.locator('input[type="file"][accept*="image"]').last
            await file_input.set_input_files(thumbnail_path)
            await page.wait_for_timeout(4000)  # 等图片加载+裁剪渲染

            # 4. 点「确定」按钮
            modal_footer = page.locator("div.d-modal-footer")
            confirm = modal_footer.get_by_text("确定", exact=True).first
            if not await confirm.count():
                confirm = page.get_by_role("button", name="确定").first
            await confirm.wait_for(state="visible", timeout=10000)
            await confirm.click()

            # 5. 等弹窗关闭
            modal = page.locator("div.d-modal")
            try:
                await modal.first.wait_for(state="hidden", timeout=15000)
            except Exception:
                pass
            xiaohongshu_logger.success(_msg("🥳", "封面已经设置完成"))
        except Exception as exc:
            xiaohongshu_logger.warning(_msg("🖼️", f"封面设置失败，跳过该步骤继续发布（用视频首帧）：{exc}"))
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(500)
            except Exception:
                pass

    # 哪些步骤失败必须中止发布；其余步骤失败只告警，继续努力把笔记发出去。
    # 注意 set_visibility / set_original / set_schedule 必须中止：
    # 用户明确要求"仅自己可见"却发成了公开，或要求定时却立即发出去，都是不可接受的后果。
    FATAL_STEPS = frozenset(
        {
            "open_publish_page",
            "upload_video",
            "wait_upload_done",
            "fill_title",
            "set_visibility",
            "set_original",
            "set_schedule",
        }
    )

    # 发布的每一步都会追加到这里，供 Web 端逐条展示
    step_results: list[OpResult]

    async def upload_video_content(self, page: Page) -> None:
        """视频发布编排（at-ai-xhs 改造版）。

        与上游的差异：
        - 每一步都走 `page_ops` 的**命名操作**，失败时能明确指出坏在哪一步；
        - **等待上传有超时上限**（上游是 `while True` 无上限，卡住会永远转下去）；
        - **提交有重试上限**（上游也是无上限的 `while True`）；
        - 新增：**合集选择**、**可见范围**、**原创声明**、**dry-run 演练**、**发布后校验**；
        - 失败步骤自动留全页截图，排障不用重新复现。
        """
        ops = XhsPageOps(page, debug=self.debug, dry_run=self.dry_run)
        scheduled = (
            self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED
            and self.publish_date != 0
        )
        schedule_at = self.publish_date if isinstance(self.publish_date, datetime) else None
        if scheduled and schedule_at is None:
            # publish_date 是毫秒时间戳的形式，转换一下
            try:
                schedule_at = datetime.fromtimestamp(int(self.publish_date) / 1000)
            except (TypeError, ValueError):
                schedule_at = None

        self.step_results = []

        async def step(coro, *, fatal: bool | None = None) -> OpResult:
            """执行一步并记录。fatal 则失败即中止整条发布。"""
            result = await coro
            self.step_results.append(result)
            should_abort = fatal if fatal is not None else (result.step in self.FATAL_STEPS)
            if result.failed:
                xiaohongshu_logger.error(f"❌ [{result.step}] {result.message}")
                if should_abort:
                    await ops.screenshot(f"abort-{result.step}")
                    raise RuntimeError(
                        f"发布中止于步骤 [{result.step}]: {result.message}"
                    )
            else:
                xiaohongshu_logger.info(f"{'✅' if result.ok else '⚠️'} [{result.step}] {result.message}")
            return result

        xiaohongshu_logger.info(_msg("🏃", f"小人开始搬运视频: {self.title}.mp4"))
        if self.dry_run:
            xiaohongshu_logger.warning(_msg("🧪", "dry-run 模式：会走完所有步骤，但不会真的点提交按钮"))

        # ---- 打开页面 / 上传 ----
        await step(ops.open_publish_page())
        await step(ops.select_video_tab())
        await step(ops.upload_video(self.file_path))
        await step(ops.wait_upload_done(timeout=self.upload_timeout))

        # ---- 内容 ----
        await step(ops.fill_title(self.title))
        await step(ops.fill_desc(self.desc))
        await step(ops.fill_tags(self.tags))

        # ---- 合集（本项目的核心增量；找不到控件/没匹配上都不算失败，保持不选）----
        await step(ops.select_collection(self.collection_name), fatal=False)

        # ---- 封面（沿用上游实现）----
        if self.thumbnail_path:
            try:
                await self.set_thumbnail(page, self.thumbnail_path)
                self.step_results.append(OpResult("set_thumbnail", "ok", "封面已设置"))
                xiaohongshu_logger.info("✅ [set_thumbnail] 封面已设置")
            except Exception as exc:
                self.step_results.append(OpResult("set_thumbnail", "failed", str(exc)))
                xiaohongshu_logger.warning(f"⚠️ [set_thumbnail] 设置封面失败，继续发布: {exc}")

        # ---- 发布设置（这三项的失败会导致"发错"，所以是致命步骤）----
        await step(ops.set_original(self.is_original))
        await step(ops.set_visibility(self.visibility))
        if scheduled:
            await step(ops.set_schedule(schedule_at))

        # ---- 提交 ----
        submitted = await step(ops.submit(scheduled=scheduled), fatal=True)

        # ---- 发布后校验 ----
        if submitted.ok and not scheduled:
            await step(ops.verify_published(self.title), fatal=False)

        # ---- 收尾汇总 ----
        failed = [r for r in self.step_results if r.failed]
        skipped = [r for r in self.step_results if r.skipped]
        xiaohongshu_logger.info(
            _msg(
                "📋",
                f"流程结束：成功 {len(self.step_results) - len(failed) - len(skipped)} 步 / "
                f"降级 {len(skipped)} 步 / 失败 {len(failed)} 步",
            )
        )
        for r in skipped:
            xiaohongshu_logger.warning(f"   ⚠️ {r.step}: {r.message}")
        if self.dry_run:
            xiaohongshu_logger.success(_msg("🧪", "dry-run 完成：全部步骤通过，未实际发布"))
        elif submitted.ok:
            xiaohongshu_logger.success(_msg("🥳", "视频发布成功，小人开心收工"))

    async def upload(self, playwright: Playwright) -> None:
        xiaohongshu_logger.info(_msg("🧍", "小人先检查 cookie、视频文件、封面和发布时间"))
        await self.validate_upload_args()
        xiaohongshu_logger.info(_msg("🥳", "上传前检查通过"))
        browser = await playwright.chromium.launch(headless=self.headless, channel="chromium")
        context = await browser.new_context(
            permissions=["geolocation"],
            storage_state=self.account_file,
        )
        context = await set_init_script(context)

        try:
            page = await context.new_page()
            await self.upload_video_content(page)
            await context.storage_state(path=self.account_file)
            xiaohongshu_logger.success(_msg("🥳", "cookie 更新完毕"))
        finally:
            await context.close()
            await browser.close()

    async def xiaohongshu_upload_video(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)

    async def main(self):
        await self.xiaohongshu_upload_video()


class XiaoHongShuNote(XiaoHongShuBaseUploader):
    def __init__(
        self,
        image_paths,
        note,
        tags,
        publish_date: datetime | int,
        account_file,
        title: str | None = None,
        desc: str | None = None,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
        )
        self.image_paths = image_paths
        self.note = note or ""
        self.tags = tags or []
        self.desc = desc if desc is not None else self.note
        self.title = title or ((self.desc or self.note)[:20] if (self.desc or self.note) else "")

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.image_paths:
            raise ValueError("图文模式下，图片是必须的")
        if not self.title or not str(self.title).strip():
            raise ValueError("图文模式下，title 是必须的")

        if isinstance(self.image_paths, (str, Path)):
            self.image_paths = [self.image_paths]

        normalized_image_paths = []
        for image_path in self.image_paths:
            normalized_image_paths.append(str(self.validate_image_file(image_path)))
        self.image_paths = normalized_image_paths

    async def upload_note_content(self, page: Page) -> None:
        xiaohongshu_logger.info(_msg("🏃", f"小人开始搬运图文，共 {len(self.image_paths)} 张图片"))
        xiaohongshu_logger.info(_msg("🧭", "小人正在赶往图文发布页"))
        publish_url = _build_xhs_creator_url(
            "/publish/publish?from=homepage&target=image"
        )
        await page.goto(publish_url)
        await page.wait_for_url(publish_url)

        upload_input = page.locator('input[type="file"][accept*="image"]').first
        if not await upload_input.count():
            upload_input = page.locator("div[class^='upload-content'] input[class='upload-input']").first

        await upload_input.wait_for(state="attached", timeout=30000)
        xiaohongshu_logger.info(_msg("📤", "小人正在上传图片"))
        await upload_input.set_input_files(self.image_paths)

        while True:
            try:
                title_container = page.locator('input[placeholder*="填写标题"]').first
                await title_container.wait_for(state="visible", timeout=3000)
                xiaohongshu_logger.success(_msg("🥳", "图文素材已经传完，可以开始填写内容了"))
                break
            except Exception:
                xiaohongshu_logger.debug(_msg("🧍", "图文素材还在上传，小人继续等一会"))
                await asyncio.sleep(1)

        xiaohongshu_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_meta(page)

        await self.check_original_declaration(page)

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_xiaohongshu(page, self.publish_date)

        while True:
            try:
                if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED:
                    await page.locator('button:has-text("定时发布")').click()
                else:
                    await page.locator('button:has-text("发布")').click()
                await page.wait_for_url(
                    XHS_PUBLISH_SUCCESS_URL_PATTERN,
                    timeout=3000
                )
                xiaohongshu_logger.success(_msg("🥳", "图文发布成功，小人开心收工"))
                break
            except Exception:
                xiaohongshu_logger.info(_msg("🏃", "小人正在冲刺发布图文"))
                if self.debug:
                    await page.screenshot(full_page=True)
                await asyncio.sleep(0.5)

    async def upload(self, playwright: Playwright) -> None:
        xiaohongshu_logger.info(_msg("🧍", "小人先检查 cookie、图片和发布时间"))
        await self.validate_upload_args()
        xiaohongshu_logger.info(_msg("🥳", "图文上传前检查通过"))
        browser = await playwright.chromium.launch(headless=self.headless, channel="chromium")
        context = await browser.new_context(
            permissions=["geolocation"],
            storage_state=self.account_file,
        )
        context = await set_init_script(context)

        try:
            page = await context.new_page()
            await self.upload_note_content(page)
            await context.storage_state(path=self.account_file)
            xiaohongshu_logger.success(_msg("🥳", "cookie 更新完毕"))
        finally:
            await context.close()
            await browser.close()

    async def xiaohongshu_upload_note(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)

    async def main(self):
        await self.xiaohongshu_upload_note()


# --------------------------------------------------------------------------- #
# 模块级入口（at-ai-xhs 改造新增）——给 CLI 和 Web 后端共用
# --------------------------------------------------------------------------- #


async def _with_creator_page(account_file: str, headless: bool, work):
    """开一个带登录态的浏览器，把 page 交给 work(page) 用，然后收尾。

    自检、列合集这类"只读"操作共用这套样板，避免每个入口都抄一遍浏览器开关逻辑。
    """
    if not os.path.exists(account_file):
        raise RuntimeError(
            f"cookie 文件不存在: {account_file}，请先完成扫码登录"
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless, channel="chromium")
        context = await browser.new_context(storage_state=account_file)
        context = await set_init_script(context)
        try:
            page = await context.new_page()
            return await work(page)
        finally:
            await context.close()
            await browser.close()


async def xiaohongshu_selftest(
    account_file: str,
    *,
    headless: bool = True,
    pages: tuple[str, ...] = ("login", "publish"),
    probe_video: str | None = None,
) -> SelftestReport:
    """探测小红书所有页面契约是否还在，**不提交任何内容**。

    两种模式：
    - **浅自检**（默认）：只打开视频发布页。能验证上传区，但标题/正文/话题/定时/合集/
      发布按钮这些控件**要上传视频之后才渲染**，所以浅自检下它们必然报缺失。
    - **深自检**（传 `probe_video`）：先真的把测试视频传上去，把编辑器撑出来，再逐条探测。
      上传 ≠ 发布：视频只会停在草稿表单里，不会发出去。

    想确认合集控件长什么样，必须用深自检。
    """

    async def work(page: Page) -> SelftestReport:
        ops = XhsPageOps(page)
        await ops.goto(S.PUBLISH_ENTRY_PATH)

        # 等页面骨架出现（SPA 首屏较慢）
        tab, _ = await ops.find(S.PUBLISH[0], timeout=30)  # publish_tab_video
        if await ops.is_login_page():
            raise RuntimeError("登录态失效，自检结果不可信，请先重新扫码登录")
        if tab is None:
            raise RuntimeError("发布页 30 秒内没有渲染出 Tab，可能是网络问题或页面大改版")

        # 切到「上传视频」（默认就是激活态，见 page_ops.select_video_tab）
        await ops.select_video_tab()

        # 等上传区就绪
        deadline = asyncio.get_event_loop().time() + 30
        while asyncio.get_event_loop().time() < deadline:
            ready, _ = await ops.find(S.PUBLISH[1], timeout=0)  # upload_input
            if ready is not None:
                break
            await asyncio.sleep(1)

        deep = bool(probe_video) and os.path.isfile(str(probe_video))
        if deep:
            xiaohongshu_logger.info(_msg("🧪", f"深自检：先上传测试视频 {probe_video}（不会发布）"))
            up = await ops.upload_video(str(probe_video))
            if up.failed:
                raise RuntimeError(f"深自检上传测试视频失败: {up.message}")
            # 等转码真正完成——"发布设置"区块（定时/合集/发布按钮）要等视频就绪才渲染，
            # 只等标题框会出现会探得太早，把真实存在的控件误报成缺失。
            await ops.wait_upload_done(timeout=300, poll=3)
            await asyncio.sleep(5)

        await asyncio.sleep(2)  # 再稳一下，等次要控件挂载

        report = await ops.selftest(pages=pages)
        report.url = page.url
        report.deep = deep
        return report

    return await _with_creator_page(account_file, headless, work)


async def xiaohongshu_list_collections(
    account_file: str,
    *,
    headless: bool = True,
    probe_video: str | None = None,
) -> OpResult:
    """打开视频发布页，只读地列出该账号可选的合集（不选中任何一项）。

    ⚠️ 合集控件**只有在上传视频、进入编辑态之后才会渲染**，所以不传 `probe_video`
    基本读不到东西。传了测试视频时会先上传（不会发布），再展开合集下拉读取选项。

    Web 端用它做"从真实合集里挑一个"，比让用户手打合集名靠谱；
    同时也回答了"这个账号到底有没有合集功能"——这是全网开源实现的空白。
    """

    async def work(page: Page) -> OpResult:
        ops = XhsPageOps(page)
        await ops.goto(S.PUBLISH_VIDEO_PATH)

        if await ops.is_login_page():
            return OpResult("list_collections", "failed", "登录态失效，请先重新扫码登录")

        await ops.select_video_tab()
        ready, _ = await ops.find(S.PUBLISH[1], timeout=30)  # upload_input
        if ready is None:
            return OpResult("list_collections", "failed", "发布页没有渲染出上传区")

        if probe_video and os.path.isfile(str(probe_video)):
            xiaohongshu_logger.info(_msg("🧪", f"先上传测试视频 {probe_video} 以露出合集控件（不会发布）"))
            up = await ops.upload_video(str(probe_video))
            if up.failed:
                return OpResult("list_collections", "failed", f"上传测试视频失败: {up.message}")
            await ops.wait_upload_done(timeout=300, poll=3)
            await asyncio.sleep(5)

        return await ops.list_collections(trigger_timeout=60)

    return await _with_creator_page(account_file, headless, work)
