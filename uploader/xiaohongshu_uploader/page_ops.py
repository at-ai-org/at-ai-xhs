# -*- coding: utf-8 -*-
"""小红书创作服务平台 —— 页面操作层。

设计目标
--------
把"在发布页上做的每一件事"变成**一个命名操作**：可单独调用、可单独重试、
可单独截图取证、失败时能明确说出坏在哪一步。

分层
----
    selectors.py   契约（控件长什么样，多候选回退）
    page_ops.py    操作（找到之后做什么）    ← 本文件
    main.py        编排（一次发布的完整流程 + 多账号/cookie 会话管理）

约定
----
- 所有操作返回 `OpResult`，**不抛异常**（除编程错误）。调用方决定是中断还是降级。
- `dry_run=True` 时，`submit()` 只做预检不真点发布按钮。
- 失败一律留全页截图，落在 `screenshots/`，排障时不用重新复现。
- 平台硬约束（标题 20 字 / 正文 1000 字 / 话题 10 个）在输入前就截断，
  并且尽量读平台自带的计数提示做二次确认。
"""

from __future__ import annotations

import asyncio
import difflib
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from patchright.async_api import Locator
from patchright.async_api import Page

from utils.log import xiaohongshu_logger

from . import selectors as S


# --------------------------------------------------------------------------- #
# 结果类型
# --------------------------------------------------------------------------- #

STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"


@dataclass
class OpResult:
    """单步操作的结果。不抛异常，用状态码表达。"""

    step: str
    status: str
    message: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def skipped(self) -> bool:
        return self.status == STATUS_SKIPPED

    @property
    def failed(self) -> bool:
        return self.status == STATUS_FAILED

    def __str__(self) -> str:
        mark = {STATUS_OK: "✅", STATUS_SKIPPED: "⚠️", STATUS_FAILED: "❌"}.get(self.status, "?")
        return f"{mark} [{self.step}] {self.message}"

    def to_dict(self) -> dict:
        """给 Web API 用的可序列化形式。"""
        return {
            "step": self.step,
            "status": self.status,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class SelftestItem:
    name: str
    page: str
    required: bool
    found: bool
    matched_selector: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "page": self.page,
            "required": self.required,
            "found": self.found,
            "matched_selector": self.matched_selector,
            "note": self.note,
        }


@dataclass
class SelftestReport:
    items: list[SelftestItem] = field(default_factory=list)
    url: str = ""
    # 是否为深自检（先上传了测试视频，把编辑器撑出来后才探测）
    deep: bool = False

    @property
    def missing_required(self) -> list[SelftestItem]:
        return [i for i in self.items if i.required and not i.found]

    @property
    def missing_optional(self) -> list[SelftestItem]:
        return [i for i in self.items if not i.required and not i.found]

    @property
    def hit_count(self) -> int:
        return sum(1 for i in self.items if i.found)

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "deep": self.deep,
            "total": len(self.items),
            "hit": self.hit_count,
            "ok": not self.missing_required,
            "items": [i.to_dict() for i in self.items],
            "missing_required": [i.name for i in self.missing_required],
            "missing_optional": [i.name for i in self.missing_optional],
        }

    def render(self) -> str:
        lines = ["", "=" * 72, "小红书页面契约自检报告", f"页面: {self.url}", "=" * 72]
        lines.append(f"{'契约':<24}{'页面':<10}{'必填':<6}结果")
        lines.append("-" * 72)
        for i in self.items:
            mark = "✅ 命中" if i.found else ("❌ 缺失" if i.required else "▫️  无(可选)")
            lines.append(f"{i.name:<24}{i.page:<10}{'是' if i.required else '否':<6}{mark}")
        lines.append("-" * 72)
        lines.append(f"命中 {self.hit_count}/{len(self.items)}")
        if self.missing_required:
            lines.append("")
            lines.append("❌ 必填契约缺失（页面大概率已改版，改 selectors.py 对应条目的 candidates）:")
            for i in self.missing_required:
                lines.append(f"   - {i.name}  ({i.page})  {i.note}")
        if self.missing_optional:
            lines.append("")
            lines.append("▫️  可选契约缺失（通常正常，可能是账号未开通该权益）:")
            for i in self.missing_optional:
                lines.append(f"   - {i.name}  {i.note}")
        lines.append("=" * 72)
        return "\n".join(lines)


# 浮层里不是"合集选项"的条目——实测底部的「创建合集」入口就长这样，
# 如果不排除，会被当成一个名叫「创建合集」的合集去匹配。
_COLLECTION_NON_OPTIONS = ("创建合集", "新建合集", "管理合集", "添加合集")


def _filter_collection_options(options: list[str]) -> list[str]:
    """过滤掉浮层里的非选项条目（如底部的「创建合集」）。"""
    return [o for o in options if o and o not in _COLLECTION_NON_OPTIONS]


# --------------------------------------------------------------------------- #
# 页面操作
# --------------------------------------------------------------------------- #


class XhsPageOps:
    """一页小红书创作服务平台的"遥控器"。

    典型用法::

        ops = XhsPageOps(page, debug=True)
        r = await ops.open_publish_page()
        r = await ops.upload_video("/abs/path/v.mp4")
        r = await ops.fill_title("标题")
        r = await ops.select_collection("成都探店")
        r = await ops.submit(scheduled=True)
    """

    def __init__(
        self,
        page: Page,
        *,
        debug: bool = False,
        dry_run: bool = False,
        screenshot_dir: str | Path = "screenshots",
    ) -> None:
        self.page = page
        self.debug = debug
        self.dry_run = dry_run
        self.screenshot_dir = Path(screenshot_dir)
        self._screenshot_dir_ready = False
        # macOS 的"全选"是 Cmd+A，Windows/Linux 是 Ctrl+A。
        # 上游写死了 "Control+KeyA"，在 Mac 上无效——这里做成跨平台。
        self.modifier = "Meta" if sys.platform == "darwin" else "Control"

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #

    def _log(self, emoji: str, text: str) -> None:
        xiaohongshu_logger.info(f"{emoji} {text}")

    def _warn(self, emoji: str, text: str) -> None:
        xiaohongshu_logger.warning(f"{emoji} {text}")

    async def screenshot(self, tag: str) -> str:
        """失败取证：留下全页截图。排障时不用重新复现。"""
        if not self._screenshot_dir_ready:
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            self._screenshot_dir_ready = True
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in tag)
        path = self.screenshot_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{safe}.png"
        try:
            await self.page.screenshot(path=str(path), full_page=True)
            self._warn("📸", f"已保存截图: {path}")
            return str(path)
        except Exception as exc:  # 截图失败绝不能影响主流程
            self._warn("📸", f"截图失败: {exc}")
            return ""

    async def _first_visible(
        self,
        candidates: Sequence[str],
        *,
        timeout: float = 0.0,
        poll_ms: int = 250,
        require_visible: bool = True,
    ) -> tuple[Locator | None, str]:
        """按优先级尝试候选选择器，返回第一个可用的。

        timeout > 0 时会在超时前轮询（避免 Vue 渲染时序问题）。

        require_visible=False 时不要求元素可见，只要**存在于 DOM** 就算命中——
        文件上传框这类隐藏元素必须走这条路径，否则永远找不到。
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            for sel in candidates:
                try:
                    loc = self.page.locator(sel).first
                    if not await loc.count():
                        continue
                    if require_visible and not await loc.is_visible():
                        continue
                    return loc, sel
                except Exception:
                    continue
            if timeout <= 0 or asyncio.get_event_loop().time() >= deadline:
                return None, ""
            await asyncio.sleep(poll_ms / 1000)

    async def find(self, contract: S.Contract, *, timeout: float = 0.0) -> tuple[Locator | None, str]:
        """按契约查找控件。可见性要求由契约自身声明。"""
        return await self._first_visible(
            contract.candidates,
            timeout=timeout,
            require_visible=contract.require_visible,
        )

    async def _find_by_text(self, texts: Iterable[str], *, timeout: float = 0.0) -> tuple[Locator | None, str]:
        """按页面文案定位（比 class 稳，但文案本身也会变）。"""
        candidates = tuple(f"text={t}" for t in texts)
        return await self._first_visible(candidates, timeout=timeout)

    async def _js_click(self, locator: Locator) -> bool:
        """用原生 JS click 触发点击。

        小红书很多可点元素的文字在 <span class="d-text"> 里且 pointer-events 被禁用，
        Playwright 的常规 click 会一直重试到超时。用原生 click 冒泡触发 Vue 事件更可靠。
        """
        try:
            handle = await locator.element_handle()
            if handle is None:
                return False
            await self.page.evaluate(
                """(el) => {
                    let node = el;
                    for (let i = 0; i < 4 && node; i++) {
                        try { node.click(); } catch (e) {}
                        node = node.parentElement;
                    }
                }""",
                handle,
            )
            return True
        except Exception:
            return False

    async def _click(self, locator: Locator, *, fallback_js: bool = True) -> bool:
        """先试 Playwright click，失败再用 JS click 兜底。"""
        try:
            await locator.click(timeout=5000)
            return True
        except Exception as exc:
            if not fallback_js:
                self._warn("🖱️", f"常规点击失败: {exc}")
                return False
            self._warn("🖱️", f"常规点击失败，改用 JS 点击: {exc}")
            return await self._js_click(locator)

    async def _press_escape(self) -> None:
        """收起点开的浮层/下拉，保持"什么都没选"的状态。"""
        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 1. 页面导航与登录态
    # ------------------------------------------------------------------ #

    async def goto(self, path: str) -> OpResult:
        from .main import _build_xhs_creator_url  # 复用上游的 base_url 环境变量逻辑

        url = _build_xhs_creator_url(path)
        try:
            await self.page.goto(url)
        except Exception as exc:
            return OpResult("goto", STATUS_FAILED, f"打开 {url} 失败: {exc}", {"url": url})
        return OpResult("goto", STATUS_OK, f"已打开 {url}", {"url": url})

    async def is_login_page(self) -> bool:
        """是否被重定向到登录页。以 URL + 登录框双重判断。"""
        try:
            if "login" in (self.page.url or "").lower():
                return True
            loc, _ = await self._first_visible(S.LOGIN[0].candidates, timeout=1.0)
            return loc is not None
        except Exception:
            return False

    async def ensure_logged_in(self) -> OpResult:
        """发布前的登录态闸门。失效就直接返回失败，让上层去走扫码流程。"""
        r = await self.goto("/publish/publish")
        if r.failed:
            return r
        await asyncio.sleep(2)
        if await self.is_login_page():
            await self.screenshot("login-required")
            return OpResult(
                "ensure_logged_in",
                STATUS_FAILED,
                "登录态失效（被重定向到登录页），请先重新扫码登录",
                {"url": self.page.url},
            )
        return OpResult("ensure_logged_in", STATUS_OK, "登录态有效", {"url": self.page.url})

    # ------------------------------------------------------------------ #
    # 2. 打开发布页、切到视频模式
    # ------------------------------------------------------------------ #

    async def open_publish_page(self) -> OpResult:
        r = await self.goto(S.PUBLISH_VIDEO_PATH)
        if r.failed:
            return r
        # 等上传控件出现，这是"发布页真的渲染好了"的信号
        loc, sel = await self.find(S.PUBLISH[1], timeout=30)  # upload_input
        if loc is None:
            await self.screenshot("publish-page-not-ready")
            return OpResult("open_publish_page", STATUS_FAILED, "发布页已打开但上传控件未出现")
        return OpResult("open_publish_page", STATUS_OK, f"发布页就绪（上传控件: {sel}）")

    async def select_video_tab(self) -> OpResult:
        """切到「上传视频」。若已在视频模式则直接成功。

        实测要点：该 Tab 默认就是激活态（class 含 `active`），此时点击会被内部的
        `<span class="title">` 拦截指针事件而超时——所以先判断是否已激活，需要点时才用 force。
        """
        loc, sel = await self.find(S.PUBLISH[0], timeout=10)  # publish_tab_video
        if loc is None:
            # 直接带 target=video 进来时可能没有 Tab，不算失败
            return OpResult("select_video_tab", STATUS_SKIPPED, "未找到视频 Tab，假定已在视频模式")

        try:
            cls = (await loc.get_attribute("class")) or ""
        except Exception:
            cls = ""
        if "active" in cls.split():
            return OpResult("select_video_tab", STATUS_OK, "「上传视频」已是激活状态，无需切换")

        try:
            await loc.click(timeout=5000, force=True)
        except Exception:
            if not await self._js_click(loc):
                return OpResult("select_video_tab", STATUS_FAILED, "点击「上传视频」Tab 失败")
        await asyncio.sleep(1)
        return OpResult("select_video_tab", STATUS_OK, f"已切换到上传视频（{sel}）")

    # ------------------------------------------------------------------ #
    # 3. 上传视频
    # ------------------------------------------------------------------ #

    async def upload_video(self, video_path: str | Path) -> OpResult:
        """设置文件到上传输入框。注意：这一步只负责"提交上传"，不负责等待。"""
        path = Path(video_path)
        if not path.is_absolute():
            return OpResult("upload_video", STATUS_FAILED, f"必须是绝对路径: {path}")
        if not path.is_file():
            return OpResult("upload_video", STATUS_FAILED, f"文件不存在: {path}")

        loc, sel = await self.find(S.PUBLISH[1], timeout=15)  # upload_input
        if loc is None:
            await self.screenshot("upload-input-missing")
            return OpResult("upload_video", STATUS_FAILED, "找不到上传输入框")
        try:
            await loc.set_input_files(str(path))
        except Exception as exc:
            await self.screenshot("upload-set-files-failed")
            return OpResult("upload_video", STATUS_FAILED, f"set_input_files 失败: {exc}")
        return OpResult(
            "upload_video",
            STATUS_OK,
            f"已提交上传 {path.name}（{path.stat().st_size / 1048576:.1f}MB）",
            {"selector": sel},
        )

    async def wait_upload_done(self, *, timeout: float = 600, poll: float = 2.0) -> OpResult:
        """等视频转码完成。

        判定方式（沿用上游经验）：读预览区域文本，命中关键词即算完成；
        兜底是"标题输入框出现了"，因为进入编辑态本身说明上传已就绪。
        **与上游不同的是这里有超时上限**——上游是 while True 无上限，卡住会永远转下去。
        """
        deadline = asyncio.get_event_loop().time() + timeout
        last_text = ""
        while asyncio.get_event_loop().time() < deadline:
            # 兜底：标题框出现 = 已进入编辑态
            title, _ = await self._first_visible(S.PUBLISH[3].candidates, timeout=0)  # title_input
            if title is not None:
                return OpResult("wait_upload_done", STATUS_OK, "视频就绪（标题框已出现）")

            preview, _ = await self._first_visible(S.PUBLISH[2].candidates, timeout=0)  # upload_preview
            if preview is not None:
                try:
                    text = await preview.inner_text()
                except Exception:
                    text = ""
                last_text = text
                if any(kw in text for kw in S.Text.UPLOAD_DONE_KEYWORDS):
                    return OpResult("wait_upload_done", STATUS_OK, "视频上传成功", {"preview": text.strip()[:200]})
            await asyncio.sleep(poll)

        await self.screenshot("upload-timeout")
        return OpResult(
            "wait_upload_done",
            STATUS_FAILED,
            f"等待视频上传/转码超时（{timeout:.0f}s）",
            {"last_preview": last_text.strip()[:200]},
        )

    # ------------------------------------------------------------------ #
    # 4. 标题 / 正文 / 话题
    # ------------------------------------------------------------------ #

    async def fill_title(self, title: str) -> OpResult:
        title = (title or "").strip()
        if not title:
            return OpResult("fill_title", STATUS_SKIPPED, "标题为空，跳过")
        truncated = title[: S.MAX_TITLE_LEN]

        loc, sel = await self.find(S.PUBLISH[3], timeout=10)  # title_input
        if loc is None:
            await self.screenshot("title-input-missing")
            return OpResult("fill_title", STATUS_FAILED, "找不到标题输入框")
        try:
            await loc.click()
            await self.page.keyboard.press(f"{self.modifier}+KeyA")
            await loc.fill(truncated)
        except Exception as exc:
            return OpResult("fill_title", STATUS_FAILED, f"填写标题失败: {exc}")

        detail = {"selector": sel, "written": truncated}
        if len(title) > S.MAX_TITLE_LEN:
            detail["truncated_from"] = len(title)
            self._warn("✂️", f"标题超过 {S.MAX_TITLE_LEN} 字，已截断")

        # 平台自带的计数提示比我们自己数字数更可信，读到就以此为准
        hint, _ = await self._first_visible(S.PUBLISH[4].candidates, timeout=0)  # title_length_hint
        if hint is not None:
            try:
                detail["platform_hint"] = (await hint.inner_text()).strip()
            except Exception:
                pass
        return OpResult("fill_title", STATUS_OK, f"标题已填: {truncated}", detail)

    async def fill_desc(self, desc: str) -> OpResult:
        desc = (desc or "").strip()
        if not desc:
            return OpResult("fill_desc", STATUS_SKIPPED, "正文为空，跳过")
        if len(desc) > S.MAX_DESC_LEN:
            self._warn("✂️", f"正文超过 {S.MAX_DESC_LEN} 字，已截断")
            desc = desc[: S.MAX_DESC_LEN]

        loc, sel = await self.find(S.PUBLISH[5], timeout=10)  # desc_input
        if loc is None:
            await self.screenshot("desc-input-missing")
            return OpResult("fill_desc", STATUS_FAILED, "找不到正文输入框")
        try:
            # 富文本编辑器：先点一下拿到焦点，再全选清空，最后逐字输入
            await loc.click()
            await self.page.keyboard.press(f"{self.modifier}+KeyA")
            await self.page.keyboard.press("Delete")
            await self.page.keyboard.type(desc)
        except Exception as exc:
            return OpResult("fill_desc", STATUS_FAILED, f"填写正文失败: {exc}")

        err, _ = await self._first_visible(S.PUBLISH[6].candidates, timeout=0)  # desc_length_error
        if err is not None:
            try:
                msg = (await err.inner_text()).strip()
                await self.screenshot("desc-too-long")
                return OpResult("fill_desc", STATUS_FAILED, f"正文超长被平台拒绝: {msg}")
            except Exception:
                pass
        return OpResult("fill_desc", STATUS_OK, f"正文已填（{len(desc)} 字）", {"selector": sel})

    async def fill_tags(self, tags: Sequence[str], *, max_tags: int = S.MAX_TAGS) -> OpResult:
        """逐个输入 #话题 并点选联想结果。

        降级策略（沿用上游踩坑经验）：话题下拉依赖平台联想接口实时返回，
        网络抖动或无匹配时会等不到。话题是**可选增强项**，等不到就跳过该话题继续，
        不让整条发布因此失败——但要**回删已键入的 "#tag" 文本**，避免它残留进正文。
        另外上游注释明确写过"标签超过 10 个会导致死循环卡住发布"，所以这里硬截断。
        """
        tags = [t.strip().lstrip("#") for t in (tags or []) if t and t.strip()]
        if not tags:
            return OpResult("fill_tags", STATUS_SKIPPED, "无话题，跳过")
        if len(tags) > max_tags:
            self._warn("🏷️", f"话题 {len(tags)} 个超过上限 {max_tags}，只取前 {max_tags} 个")
            tags = tags[:max_tags]

        written: list[str] = []
        skipped: list[str] = []
        for tag in tags:
            try:
                await self.page.keyboard.type(f"#{tag}", delay=30)
                suggest, _ = await self._first_visible(S.PUBLISH[7].candidates, timeout=6)  # topic_suggest
                if suggest is None:
                    raise RuntimeError("联想下拉未出现")
                option, _ = await self._first_visible(S.PUBLISH[8].candidates, timeout=4)  # first option
                if option is None:
                    raise RuntimeError("联想结果为空")
                await option.click()
                written.append(tag)
                await asyncio.sleep(0.3)
            except Exception as exc:
                self._warn("🏷️", f"话题「{tag}」未成词，跳过并清理残留: {exc}")
                for _ in range(len(tag) + 1):
                    await self.page.keyboard.press("Backspace")
                skipped.append(tag)

        msg = f"话题成功 {len(written)}/{len(tags)}"
        if skipped:
            msg += f"，跳过: {', '.join(skipped)}"
        detail = {"written": written, "skipped": skipped}
        if not written:
            return OpResult("fill_tags", STATUS_SKIPPED, msg, detail)
        return OpResult("fill_tags", STATUS_OK, msg, detail)

    # ------------------------------------------------------------------ #
    # 5. 合集选择（本项目相对上游的核心增量）
    # ------------------------------------------------------------------ #

    async def select_collection(self, collection_name: str | None, *, match_cutoff: float = 0.8) -> OpResult:
        """把笔记加入指定合集。

        ⚠️ 这条链路全网没有开源先例（见 docs/开源项目调研.md 4.1），DOM 属于待实测确认，
        因此实现采用"文案锚点 + 多策略 + 严格匹配"，并且**任何一步不确定都退回"保持未选"**——
        进错合集的伤害大于不进合集。

        Returns:
            status=ok      已选中并校验
            status=skipped 控件不存在（账号未开通）/ 无匹配项 / 用户明确不选
            status=failed  控件存在但操作出错（此时也保证已收起下拉、保持未选）
        """
        if not collection_name:
            return OpResult("select_collection", STATUS_SKIPPED, "未指定合集，保持不选")

        trigger, trigger_sel = await self._find_by_text(S.Text.COLLECTION_TITLE_HINTS, timeout=5)
        if trigger is None:
            await self.screenshot("collection-control-missing")
            return OpResult(
                "select_collection",
                STATUS_SKIPPED,
                "发布页没有合集控件（账号未开通合集 / 该笔记类型不支持），保持不选",
            )

        self._log("📚", f"找到合集控件（{trigger_sel}），尝试选择「{collection_name}」")
        if not await self._click(trigger):
            await self._press_escape()
            return OpResult("select_collection", STATUS_FAILED, "合集控件点击失败，保持不选")

        # 等选项列表出现
        options_loc, _ = await self._first_visible(S.COLLECTION[1].candidates, timeout=8)
        if options_loc is None:
            await self.screenshot("collection-no-options")
            await self._press_escape()
            return OpResult("select_collection", STATUS_SKIPPED, "合集下拉没有展开或没有选项，保持不选")

        try:
            texts = await options_loc.all_inner_texts()
        except Exception as exc:
            await self._press_escape()
            return OpResult("select_collection", STATUS_FAILED, f"读取合集选项失败: {exc}，保持不选")

        options = [t.strip() for t in texts if t and t.strip()]
        options = _filter_collection_options(options)
        if not options:
            await self._press_escape()
            return OpResult("select_collection", STATUS_SKIPPED, "合集选项为空，保持不选")

        # 三档匹配：精确 → 去空格相等 → 模糊（且必须超过阈值，否则宁可不选）
        target = collection_name.strip()
        idx = next((i for i, t in enumerate(options) if t == target), None)
        matched_by = "exact"
        if idx is None:
            idx = next((i for i, t in enumerate(options) if t.replace(" ", "") == target.replace(" ", "")), None)
            matched_by = "normalized"
        if idx is None:
            close = difflib.get_close_matches(target, options, n=1, cutoff=match_cutoff)
            if close:
                idx = options.index(close[0])
                matched_by = f"fuzzy({difflib.SequenceMatcher(None, target, close[0]).ratio():.2f})"

        if idx is None:
            await self.screenshot("collection-no-match")
            await self._press_escape()
            return OpResult(
                "select_collection",
                STATUS_SKIPPED,
                f"合集列表里没有足够接近「{target}」的项，保持不选（宁可空着也不进错合集）",
                {"options": options[:20]},
            )

        try:
            await options_loc.nth(idx).click()
        except Exception:
            if not await self._js_click(options_loc.nth(idx)):
                await self._press_escape()
                return OpResult("select_collection", STATUS_FAILED, "点击合集选项失败，保持不选")

        await asyncio.sleep(0.5)
        return OpResult(
            "select_collection",
            STATUS_OK,
            f"已选择合集「{options[idx]}」({matched_by})",
            {"selected": options[idx], "matched_by": matched_by, "candidates": options[:20]},
        )

    async def list_collections(self, *, trigger_timeout: float = 5) -> OpResult:
        """只读地列出发布页合集下拉里的选项（不选中任何一项）。

        给 Web 端"让用户从账号真实合集里挑一个"用——比让用户手打名字靠谱得多，
        也顺便回答了"这个账号到底有没有合集功能"。

        Args:
            trigger_timeout: 等合集控件出现的时间。它属于"发布设置"区块，
                比标题框渲染得晚（实测要等视频转码完成才出现），所以调用方常需要给大一点。
        """
        trigger, trigger_sel = await self._find_by_text(S.Text.COLLECTION_TITLE_HINTS, timeout=trigger_timeout)
        if trigger is None:
            return OpResult(
                "list_collections",
                STATUS_SKIPPED,
                "发布页没有合集控件（账号未开通合集功能）",
                {"collections": [], "trigger": ""},
            )
        if not await self._click(trigger):
            await self._press_escape()
            return OpResult("list_collections", STATUS_FAILED, "合集控件点击失败")

        options_loc, _ = await self._first_visible(S.COLLECTION[1].candidates, timeout=8)
        if options_loc is None:
            await self._press_escape()
            return OpResult(
                "list_collections",
                STATUS_SKIPPED,
                "点了「选择合集」但下拉没有出现选项（账号可能还没有创建过合集）",
                {"collections": [], "trigger": trigger_sel},
            )

        try:
            texts = await options_loc.all_inner_texts()
        except Exception as exc:
            await self._press_escape()
            return OpResult("list_collections", STATUS_FAILED, f"读取合集选项失败: {exc}")

        collections = _filter_collection_options([t.strip() for t in texts if t and t.strip()])
        await self._press_escape()
        return OpResult(
            "list_collections",
            STATUS_OK,
            f"共 {len(collections)} 个合集",
            {"collections": collections, "trigger": trigger_sel},
        )

    # ------------------------------------------------------------------ #
    # 6. 定时发布 / 可见范围 / 原创声明
    # ------------------------------------------------------------------ #

    async def set_schedule(self, publish_at: datetime | None) -> OpResult:
        """开启定时发布并填时间。平台限制：1 小时后 ~ 14 天内。"""
        if publish_at is None:
            return OpResult("set_schedule", STATUS_SKIPPED, "未指定定时时间，按立即发布处理")

        # 平台窗口校验（早于 +1h 只能立即发；晚于 +14d 平台不接受）
        delta = publish_at - datetime.now()
        if delta.total_seconds() < S.SCHEDULE_MIN_HOURS * 3600:
            return OpResult(
                "set_schedule",
                STATUS_SKIPPED,
                f"目标时间距现在不足 {S.SCHEDULE_MIN_HOURS} 小时，平台不接受定时，降级为立即发布",
            )
        if delta.days > S.SCHEDULE_MAX_DAYS:
            return OpResult(
                "set_schedule",
                STATUS_FAILED,
                f"目标时间超过平台上限 {S.SCHEDULE_MAX_DAYS} 天",
            )

        switch, switch_sel = await self.find(S.PUBLISH[9], timeout=8)  # schedule_switch
        if switch is None:
            await self.screenshot("schedule-switch-missing")
            return OpResult("set_schedule", STATUS_FAILED, "找不到定时发布开关")
        if not await self._click(switch):
            return OpResult("set_schedule", STATUS_FAILED, "定时发布开关点击失败")
        await asyncio.sleep(0.8)

        box, box_sel = await self.find(S.PUBLISH[10], timeout=8)  # schedule_input
        if box is None:
            await self.screenshot("schedule-input-missing")
            return OpResult("set_schedule", STATUS_FAILED, "找不到定时时间输入框")

        value = publish_at.strftime("%Y-%m-%d %H:%M")
        try:
            await box.click()
            await box.select_text()
            await box.fill(value)
            await self.page.keyboard.press("Enter")
        except Exception as exc:
            return OpResult("set_schedule", STATUS_FAILED, f"填写定时时间失败: {exc}")

        return OpResult(
            "set_schedule",
            STATUS_OK,
            f"已设置定时发布 {value}",
            {"selector": switch_sel, "input": box_sel, "value": value},
        )

    async def set_visibility(self, visibility: str | None) -> OpResult:
        """设置可见范围。默认（None / 公开可见）时不动页面，保持平台默认。"""
        if not visibility or visibility == S.Text.VISIBILITY_PUBLIC:
            return OpResult("set_visibility", STATUS_SKIPPED, "使用平台默认可见范围（公开可见）")
        supported = (S.Text.VISIBILITY_PRIVATE, S.Text.VISIBILITY_FRIENDS)
        if visibility not in supported:
            return OpResult("set_visibility", STATUS_FAILED, f"不支持的可见范围: {visibility}，可选 {supported}")

        select, _ = await self.find(S.PUBLISH[11], timeout=8)  # visibility_select
        if select is None:
            await self.screenshot("visibility-select-missing")
            return OpResult("set_visibility", STATUS_FAILED, "找不到可见范围下拉框")
        if not await self._click(select):
            return OpResult("set_visibility", STATUS_FAILED, "可见范围下拉框点击失败")
        await asyncio.sleep(0.5)

        options, _ = await self.find(S.PUBLISH[12], timeout=6)  # visibility_option
        if options is None:
            await self._press_escape()
            return OpResult("set_visibility", STATUS_FAILED, "可见范围选项未出现")
        try:
            texts = await options.all_inner_texts()
        except Exception as exc:
            await self._press_escape()
            return OpResult("set_visibility", STATUS_FAILED, f"读取可见范围选项失败: {exc}")

        for i, t in enumerate(texts):
            if visibility in (t or ""):
                if await self._click(options.nth(i)):
                    await asyncio.sleep(0.3)
                    return OpResult("set_visibility", STATUS_OK, f"可见范围已设为 {visibility}")
        await self._press_escape()
        return OpResult("set_visibility", STATUS_FAILED, f"下拉里没找到「{visibility}」，保持默认公开")

    async def set_original(self, enabled: bool) -> OpResult:
        """原创声明（带二次确认弹窗）。

        默认不声明——**只在内容确实原创时才应该开**，滥用原创声明是平台打击对象。
        """
        if not enabled:
            return OpResult("set_original", STATUS_SKIPPED, "未请求原创声明，保持关闭")

        cards = self.page.locator(S.PUBLISH[13].candidates[0])
        count = await cards.count()
        for i in range(count):
            card = cards.nth(i)
            try:
                text = await card.inner_text()
            except Exception:
                continue
            if S.Text.ORIGINAL_CARD not in text:
                continue
            switch = card.locator("div.d-switch").first
            if not await switch.count():
                continue
            try:
                checked = await switch.evaluate(
                    "el => { const i = el.querySelector('input[type=checkbox]'); return i ? i.checked : false; }"
                )
            except Exception:
                checked = False
            if checked:
                return OpResult("set_original", STATUS_OK, "原创声明已处于开启状态")
            if not await self._click(switch):
                return OpResult("set_original", STATUS_FAILED, "原创声明开关点击失败")
            await asyncio.sleep(0.8)

            # 二次确认弹窗：勾须知 → 点「声明原创」
            confirm = await self._confirm_original_dialog()
            if not confirm:
                await self.screenshot("original-confirm-failed")
                return OpResult("set_original", STATUS_FAILED, "原创声明确认弹窗处理失败")
            return OpResult("set_original", STATUS_OK, "已声明原创")

        await self.screenshot("original-card-missing")
        return OpResult("set_original", STATUS_SKIPPED, "页面没有原创声明选项（账号未开通），跳过")

    async def _confirm_original_dialog(self) -> bool:
        try:
            footers = self.page.locator("div.footer")
            n = await footers.count()
            target = None
            for i in range(n):
                f = footers.nth(i)
                try:
                    if S.Text.ORIGINAL_CONFIRM in (await f.inner_text()):
                        target = f
                        break
                except Exception:
                    continue
            if target is None:
                return False

            checkbox = target.locator("div.d-checkbox").first
            if await checkbox.count():
                checked = await checkbox.evaluate(
                    "el => (el.querySelector('input[type=checkbox]')||{}).checked || !!el.querySelector('.checked')"
                )
                if not checked:
                    await self._click(checkbox)
                    await asyncio.sleep(0.5)

            btn = target.locator("button.custom-button").first
            if not await btn.count():
                btn = target.locator("button").first
            if not await btn.count():
                return False
            return await self._click(btn)
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # 7. 提交与校验
    # ------------------------------------------------------------------ #

    async def submit(self, *, scheduled: bool = False) -> OpResult:
        """点发布按钮。

        dry_run 模式下**不点**，只确认按钮可点——这是验证前序所有步骤是否正确的安全阀。
        """
        contract = S.PUBLISH[15] if scheduled else S.PUBLISH[14]  # schedule_button / publish_button
        loc, sel = await self.find(contract, timeout=15)
        if loc is None:
            await self.screenshot("publish-button-missing")
            return OpResult("submit", STATUS_FAILED, f"找不到提交按钮（{'定时发布' if scheduled else '发布'}）")

        if self.dry_run:
            return OpResult("submit", STATUS_SKIPPED, f"dry-run：已确认提交按钮可用（{sel}），未实际点击")

        try:
            await loc.click()
        except Exception:
            if not await self._js_click(loc):
                await self.screenshot("publish-click-failed")
                return OpResult("submit", STATUS_FAILED, "提交按钮点击失败")

        # 平台自带校验（字数/违规）会阻止跳转，所以成功判定必须看 URL 有没有走
        try:
            await self.page.wait_for_url(S.PUBLISH_SUCCESS_URL_PATTERN, timeout=15000)
            return OpResult("submit", STATUS_OK, "已提交，页面跳转到发布成功页")
        except Exception:
            await self.screenshot("publish-not-confirmed")
            return OpResult(
                "submit",
                STATUS_FAILED,
                "点击后未跳转到成功页——可能被平台校验拦下或仍在异步处理，请查看截图确认",
            )

    async def verify_published(self, title: str, *, timeout_ms: int = 20000) -> OpResult:
        """发布后校验：到「笔记管理」核对笔记真的存在。

        上游只等 URL 跳转就算成功，存在"点了发布但没发出去"的假成功风险。
        这一步是补上第二重确认。

        注意：定时发布的笔记通常**不会立刻**出现在已发布列表（要等审核+到点），
        所以本方法对定时任务返回 skipped 而不是 failed，避免误报。
        """
        title = (title or "").strip()
        if not title:
            return OpResult("verify_published", STATUS_SKIPPED, "无标题，跳过校验")

        for path in S.NOTE_MANAGER_PATHS:
            r = await self.goto(path)
            if r.failed:
                continue
            try:
                await self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except Exception:
                pass
            if await self.is_login_page():
                return OpResult("verify_published", STATUS_FAILED, "校验时登录态失效")
            try:
                body = await self.page.inner_text("body")
            except Exception:
                continue
            # 列表里标题可能被截断，用前 10 个字做前缀匹配
            probe = title[:10]
            if probe and probe in body:
                return OpResult(
                    "verify_published",
                    STATUS_OK,
                    f"已在笔记管理页找到「{probe}…」",
                    {"url": self.page.url},
                )
            self._log("🔍", f"{path} 未找到该笔记，继续尝试下一个入口")

        await self.screenshot("verify-published-not-found")
        return OpResult(
            "verify_published",
            STATUS_SKIPPED,
            "笔记管理页未找到该笔记（定时笔记需审核通过后才入库，属正常；若是立即发布则需人工确认）",
        )

    # ------------------------------------------------------------------ #
    # 8. 自检
    # ------------------------------------------------------------------ #

    async def selftest(self, *, pages: Sequence[str] = ("login", "publish")) -> SelftestReport:
        """逐个探测所有页面契约是否还存在。

        **不提交任何内容**，只做存在性检查。页面改版后跑一次就知道坏在哪条契约上，
        不用等真实发布失败才发现。
        """
        report = SelftestReport(url=self.page.url)
        for contract in S.ALL_CONTRACTS:
            if contract.page not in pages:
                continue
            loc, matched = await self._first_visible(
                contract.candidates, timeout=0, require_visible=contract.require_visible
            )
            report.items.append(
                SelftestItem(
                    name=contract.name,
                    page=contract.page,
                    required=contract.required,
                    found=loc is not None,
                    matched_selector=matched,
                    note=contract.note,
                )
            )
        return report


__all__ = [
    "XhsPageOps",
    "OpResult",
    "SelftestReport",
    "SelftestItem",
    "STATUS_OK",
    "STATUS_SKIPPED",
    "STATUS_FAILED",
]
