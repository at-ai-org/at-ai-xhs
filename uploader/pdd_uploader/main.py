# -*- coding: utf-8 -*-
"""拼多多视频（多多视频）发布驱动（at-ai-xhs 改造新增）。

为什么是"页面自动化"而不是直连接口
----------------------------------
拼多多开放平台（`open.pinduoduo.com`）只有电商侧能力（商品 / 订单 / 物流 / 多多进宝），
**没有视频发布接口**；能发布视频的只有官方 PC 后台 `live.pinduoduo.com`。而这个后台
所有 `/api/backbone/*` 请求都带平台自己算的签名（上传还要额外带 `anti-content` 头），
纯 HTTP 复刻签名属于"逆向 + 长期维护"，风险收益都不划算。

所以走**和快手同一条路线**：开一个浏览器、带着账号 cookie、像人一样填表点按钮。
签名交给页面自己算，页面改版时我们只需要改选择器。

角色说明（重要）
----------------
拼多多这一套后台有三种身份，登录方式各不相同，**本项目只做「达人」**：

* **达人**：`/login/anchor`，App 扫码（token_type=2）——本项目支持；
* 商家：账号密码 / 短信验证码——不做（要短信，属于"半自动"）；
* 机构：`mcn.pinduoduo.com`，另一套域名、仅企业主体——不做。

达人发布的是**达人带货视频 / 内容视频**：不挂商品也能发（就是普通内容视频），
挂商品需要先在后台通过「匹配度测试」。本版**默认不挂商品**，把发布这件事先跑通。

发布页的硬约束（从平台发布页前端产物里读出来的，不是猜的）
----------------------------------------------------------
* **视频发布没有「标题」字段**——平台的发布载荷里 `title` 只出现在图文（IMAGE）那一支，
  视频那一支是 `desc / cover / vid / scheduleTime / goodsIdList…`；发布页上那个
  placeholder=「添加标题」的输入框，渲染条件同样是 `feedType === IMAGE`。
  所以本驱动默认把发布中心的「标题」**并进描述的第一行**（见 `_compose_desc`）。
  （标题 ≤ 40 字且不能带 `#` 是**图文**的规则，这里保留是为了兜住"平台哪天给视频也加标题框"。）
* 描述 ≤ 500 字；话题写在描述里（`#话题`），单个 ≤ 15 字、**最多 5 个**；
* 描述编辑器是**富文本 contenteditable**（平台的 SABO/ACE 编辑器），不是 textarea；
* 发布按钮是**每张视频卡片上的「发布」**，点完文案变「发布中」→「已发布」；
  平台**没有"发布成功"这句提示文案**，所以成败以 `/api/backbone/video/publish`
  的接口回执为准（见 `_submit`）；
* 封面必须存在才能发布（校验文案「请添加封面」/「请将全部视频添加封面」），
  规格是 **3:4 竖版、≥720P、≤5MB**（平台文案：「需上传封面比例为3:4且分辨率大于720P；
  文件大小不超过5M」）；缺封面时驱动会自动抽视频首帧生成一张合规封面再重试一次；
* 视频：mp4 优先、1080P 及以上、帧率 ≥ 24、码率 ≥ 6Mbps、竖版更容易拿流量。

选择器的可靠性说明
------------------
拼多多的发布页**必须登录才能渲染**，所以这里的选择器不是"看着页面写的"，
而是从平台的构建产物里反推出来的，分两类来源：

  1. **平台自己的 CSS Module 类名**（`[class*="singlePublish"]` 这种）——
     类名形如 `video-list_singlePublish__UqpN1`，**可读部分每次发版都在、只有 hash 会变**；
  2. 平台前端里的**字符串常量**（「添加视频」「定时发布」「确认封面裁剪」…）。

选择器**已经用真实登录态逐条核对过**（2026-10-08，含文件输入框隐藏、上传页地址、
`video-list_success` 其实是"上传成功"这些坑，详见 `docs/拼多多视频接入方案.md` 的 1.6 节）。
平台改版后仍可随时复检：
打开 `SAU_PDD_DUMP=1`（或 `conf.py` 里把 `PDD_DEBUG_DUMP` 设为 True）会记录每一步的
HTML + 全页截图到 `screenshots/pdd/`；更直接的是先跑
`tools/verify_pdd_publish_page.py`（逐条试候选选择器并给报告）。
流程逻辑本身不依赖真实页面，已经由 `tools/verify_pdd_publish_flow_offline.py`
（本地假发布页 + 真实驱动，7 个场景）覆盖。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path

from patchright.async_api import Locator
from patchright.async_api import Page
from patchright.async_api import Playwright
from patchright.async_api import async_playwright

from conf import LOCAL_CHROME_PATH
from conf import PDD_CONTENT_DECLARATION
from conf import PDD_COVER_MAX_MB
from conf import PDD_COVER_MIN_WIDTH
from conf import PDD_COVER_RATIO
from conf import PDD_DEBUG_DUMP
from conf import PDD_DESC_MAX
from conf import PDD_HEADLESS
from conf import PDD_LOGIN_URL_MARKER
from conf import PDD_PUBLISH_URL
from conf import PDD_PUBLISH_URL_LEGACY
from conf import PDD_TITLE_MAX
from conf import PDD_UPLOAD_RETRIES
from conf import PDD_VIDEO_HINT
from conf import PDD_UPLOAD_STALL_SECONDS
from conf import PDD_TOPIC_MAX
from conf import PDD_TOPIC_MAX_COUNT
import myUtils.publish_evidence as evidence
from uploader.base_video import BaseVideoUploader
from utils.base_social_media import set_init_script
from utils.log import pdd_logger

#: 发布策略常量。和其余四个平台同名同形，`sau_backend_publish.PUBLISH_STRATEGY` 直接引用；
#: 定时发布必须**显式**传 scheduled——基类在 immediate 时会把 publish_date 清零。
PDD_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
PDD_PUBLISH_STRATEGY_SCHEDULED = "scheduled"

#: 导航 / 上传 / 提交的等待预算（毫秒或秒，见各自注释）。
PDD_NAV_TIMEOUT_MS = 90_000
PDD_NAV_ATTEMPTS = 2
PDD_UPLOAD_ENTRY_TIMEOUT_MS = 90_000
PDD_UPLOAD_ENTRY_ATTEMPTS = 2
PDD_SUBMIT_TIMEOUT_MS = 60_000
#: 等「发布」按钮**变成可点**的上限。
#:
#: 按钮可见 ≠ 可点：平台把视频收下之后还要自己"处理/转码"一会儿，这期间发布按钮是
#: 禁用态的（`BTN_disabled_*`）。原来拿到可见按钮就直接 click()，Playwright 默认只等
#: 30 秒就放弃，报出来的是一大段 "element is not enabled" 的点击重试日志——
#: 既看不出"平台还没处理完"，也把本来只是慢一点的那次发布判成了失败。
#: 真机上（一轮 4 个平台同时发、机器负载 40 的时候）这一步经常要一两分钟。
PDD_SUBMIT_READY_TIMEOUT_MS = 180_000
#: 点「发布」之后的"冷静期"：期间不下任何结论，只看页面有没有报错。
#: ⚠️ 这个值必须**远小于平台提示的存活时间**：平台的警告（`c.default.warn(..., 2000)`）
#: 大约只显示 2 秒，原来设的 6 秒会让"点完先睡 6 秒再检查"永远读不到那句提示——
#: 真机上表现为"点了发布什么都没发生"（实际平台早就说了「请添加封面」）。
PDD_SUBMIT_GRACE_SECONDS = 1
PDD_DEBUG_SHOT_TIMEOUT_MS = 5_000

#: 发布接口路径。发布页前端里就是 `B={VIDEO:"/api/backbone/video/publish", IMAGE:"/api/backbone/video/pictures/publish"}`，
#: 点发布之后浏览器一定会打这个接口——**这是最可靠的成败判据**（比看页面文案可靠得多，
#: 因为平台的"发布成功"根本没有文案，它只是把视频卡片的状态改成已发布）。
PDD_PUBLISH_API_PATH = "/api/backbone/video/publish"

#: 平台响应信封里的**成功码**。拼多多的接口统一回 `{success, errorCode, errorMsg, result}`，
#: 成功时 `success=true` 且 `errorCode=1000000`（实测：发布页初始化接口就是
#: `{"success": True, "errorCode": 1000000, "errorMsg": None, ...}`）。
#: ⚠️ 曾经因为"看到 errorCode 就当失败"，把两次**已经发布成功**的结果记成了失败
#: （平台上作品都在了，发布记录却写"失败"）——判读响应时千万别只看字段有没有。
PDD_RESPONSE_OK_CODES = {"0", "1000000"}

#: 每一步的候选选择器。**改选择器只改这里**，按顺序试，第一个**可见**的算数。
#:
#: 分两类来源，可信度不同：
#:   1. **平台自己的 CSS Module 类名**（`[class*="xxx"]`）——从它的构建产物里扒出来的。
#:      类名形如 `video-list_singlePublish__UqpN1`（`<文件>_<名字>__<hash>`），
#:      **可读部分每次发版都在、只有 hash 会变**，所以 `[class*="singlePublish"]` 是稳的。
#:      这比"猜 DOM 层级"强得多，也是这次能提前发现一堆问题的原因。
#:   2. 文案（`text=` / `:has-text()`）——确定性来自平台前端里的字符串常量。
#: 上传阶段平台会说的话（取自平台前端产物）。出现这些就别再等了，直接把原因抛出去。
UPLOAD_ERROR_HINTS: dict[str, str] = {
    "视频超出最大时长": "视频超过平台允许的最长时长，请换更短的素材",
    "暂不支持上传": "这个文件平台不接受（格式/时长/分辨率不符合要求）",
    "上传失败": "平台侧上传失败，建议重试",
    "网络错误": "平台侧网络错误，建议重试",
    "获取 endpoint 失败": "平台侧上传通道异常，建议稍后重试",
    "获取endpoint失败": "平台侧上传通道异常，建议稍后重试",
    "文件上传失败": "平台侧上传失败，建议重试",
    "过滤": "这批文件被平台过滤掉了（可能重复/低质/时长不符）",
}

PDD_SELECTORS: dict[str, tuple[str, ...]] = {
    # 发布页「上传视频」区域里的文件输入框。页面 accept=".mp4,.wmv,.mov,.avi,.m4v"。
    "file_input": (
        'input[type="file"][accept*=".mp4"]',
        'input[type="file"][accept*="mp4"]',
        'input[type="file"][accept*="video"]',
        "input[type=file]",
    ),
    # 点一下就把系统文件框叫出来的按钮（没有 input 落在 DOM 里时的兜底）。
    # `no-video_draggerWrap` / `AddVideo_label` 都是平台自己的上传区类名。
    "upload_button": (
        'button:has-text("添加视频")',
        '[class*="draggerWrap"]',
        '[class*="AddVideo_label"]',
        'text=添加视频',
    ),
    # 标题输入框。⚠️ 平台**只在图文（feedType=IMAGE）时渲染它**，placeholder 是「添加标题」；
    #    视频发布页上没有这个框（发布载荷里也没有 title 字段）。
    #    这里保留探测：有就填，没有就把标题并进描述。
    "title_input": (
        'input[placeholder*="添加标题"]',
        'input[placeholder*="标题"]',
    ),
    # 描述编辑器。它**不是 textarea**，是平台自带的富文本编辑器（SABO/ACE，contenteditable），
    # CSS Module 是 `text-edit_*`：`saboIpt` 是外层容器、`hackPlaceholder` 是占位文案、
    # `emptyReactSabo` 是编辑器本体。
    "desc_editor": (
        '[class*="saboIpt"] [contenteditable="true"]',
        '[class*="emptyReactSabo"]',
        '[class*="saboIpt"]',
        '[class*="text-edit_textContent"] [contenteditable="true"]',
        '[class*="textContent"] [contenteditable="true"]',
        'div[contenteditable="true"]',
        'textarea[placeholder*="描述"]',
    ),
    # 卡片上的封面入口（点开是「封面选择」弹窗）。平台文案：「编辑封面」「更换封面」。
    "cover_entry": (
        'text=编辑封面',
        'text=更换封面',
        '[class*="custom-img-pick_trigger"]',
        '[class*="imgPickWrap"]',
        'text=请添加封面',
    ),
    # 封面弹窗里的「本地上传」页签：**必须点它**才会出现图片文件输入框。
    # 弹窗默认停在「视频截取」（从视频里截帧），「本地上传」是第二个页签。
    "cover_upload_tab": (
        'text=本地上传',
        '[class*="tab"]:has-text("本地上传")',
    ),
    # 封面弹窗里的图片输入框（`accept=".jpg,.jpeg,.png"`，隐藏的）。
    "cover_file_input": (
        'input[type="file"][accept*=".jpg"]',
        'input[type="file"][accept*="image"]',
        'input[type="file"][accept*="png"]',
    ),
    # 封面裁剪弹窗里的确认按钮。平台文案：「确认封面裁剪」「确认选择封面」。
    "cover_confirm": (
        'button:has-text("确认封面裁剪")',
        'button:has-text("确认选择封面")',
        'button:has-text("确定")',
        'button:has-text("确认")',
    ),
    # **每张视频卡片自己的发布按钮**（点完文案会变成「发布中」→「已发布」）。
    # 一次任务只有一条素材，所以点它比点底部的「一键发布」精确。
    "submit_button": (
        '[class*="singlePublish"] button',
        '[class*="singlePublish"]',
        'button:has-text("一键发布")',
        'button:has-text("发布")',
    ),
    # 卡片进入"已发布"状态（DOM 兜底判据）。
    # ⚠️ **不能**用 `video-list_success` / `video-list_mask`：2026-10-08 在真实页面上核对过——
    #   * `video-list_success` 是「**视频上传成功**」的标签（上传完就在，跟发布无关），
    #     拿它判"已发布"= 每次上传完就报成功；
    #   * `video-list_mask` 是 `publishing || published` 时都会盖的遮罩，同样不区分。
    # 真正能区分的只有按钮文案：发布成功后它从「发布」变成「已发布」（发布中显示「发布中」）。
    "item_published": (
        '[class*="singlePublish"] button:has-text("已发布")',
        'button:has-text("已发布")',
    ),
    # 定时发布的时间输入框。放在 `FormItem_publishTime__*` 这一块里，
    # ⚠️ 这一块**同时包着「立即发布 / 定时发布」两个 radio**，所以候选里必须排掉
    # radio/checkbox——否则会命中 radio 并对它 fill()，报
    # `Input of type "radio" cannot be filled`（离线自测抓到过）。
    # 真实页面（2026-10-08 核对）：点「定时发布」之后才出现一个 `placeholder="选择日期"`、
    # 值形如 `2026-10-08 16:42:40` 的输入框，它落在 `PublishTimeSetting_publishTime__*` 里面；
    # 同一块里还有两个 `<input type=radio>`（立即发布/定时发布），必须排掉。
    # ⚠️ 别把「请选择」当候选：那是**商品选择器**的 placeholder，页面上一直可见，
    # 排在这里会点错控件（真实页面核对时发现的）。
    "schedule_input": (
        '[class*="publishTime"] input[type="text"]',
        '[class*="publishTime"] input:not([type="radio"]):not([type="checkbox"])',
        'input[placeholder*="选择日期"]',
        'input[placeholder*="日期"]',
        'input[placeholder*="时间"]',
    ),
    # 草稿残留弹窗（只在弹窗**内部**找「取消」，绝不全局瞎点）。
    "draft_dialog": (
        'div[class*="modal"]:has-text("是否继续发布")',
        'div[class*="Modal"]:has-text("是否继续发布")',
        'div[class*="dialog"]:has-text("是否继续发布")',
        'div[class*="modal"]:has-text("上传成功但未发布")',
    ),
    # 「内容声明」区块与它的下拉框（真实页面：`ContentDeclaration_statement__*` 里一个 `ST_` Select）。
    # 平台**必选**：不选时点「发布」只弹「请选择内容声明」，**连请求都不发**。
    "declaration_section": (
        '[class*="ContentDeclaration_statement"]',
        '[class*="ContentDeclaration"]',
    ),
    "declaration_trigger": (
        # 平台给这个 Select 打了稳定的 `data-testid`（比 class 稳），点它就能展开
        '[data-testid="beast-core-select"]',
        '[class*="ST_outerWrapper"]',
    ),
    # 下拉选项。⚠️ 选项文案**带括号说明**（「内容无需标注（作品不含AI生成、虚构、转载及营销等信息）」），
    # 所以只能按"包含"匹配，不能全等——而且必须限定在**下拉浮层**里：
    # `ContentDeclaration_title__*` 这组类名同时被"?"图标的说明浮层复用，全局找会点到说明文字上。
    "declaration_option": (
        '[class*="ST_dropdown"] :text("{label}")',
        '[class*="dropdownMain"] :text("{label}")',
        'text={label}',
    ),
    # 发布确认弹窗。真实页面（2026-10-08 核对）：点卡片上的「发布」**不会立刻提交**，
    # 而是弹出协议窗——「我已阅读并同意《拼多多短视频管理规范》」+「同意并发布」/「不同意」，
    # **勾了协议再点「同意并发布」才会真的发请求**。
    # 主按钮用平台自己的 `data-testid="beast-core-modal-ok-button"`（最稳），
    # 刻意**不**用页脚那个全局「一键发布」（`VideoListFooter_footerBtn__*`）：
    # 那是"发布所有视频"，不是"确认这一条"，误点会牵连草稿里的其它视频。
    "publish_confirm": (
        '[data-testid="beast-core-modal-ok-button"]',
        'div[class*="MDL_"] button:has-text("同意并发布")',
        'div[class*="MDL_"] button:has-text("同意")',
    ),
    # 弹窗里的协议勾选框。input 本身是 `opacity:0; width:0; height:0`（隐藏的），
    # 所以点它旁边的文案（「我已阅读并同意」）——那才是可点的目标。
    "publish_consent": (
        'text=我已阅读并同意',
        '[class*="CBX_textWrapper"]',
        '[class*="MDL_"] label',
    ),
}


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


#: 错误信息里给用户看的现场路径（`screenshots/pdd/`）。发布失败时 `_publish` 的
#: 兜底分支一定会往这里落一份现场，所以这条提示在任何失败路径上都是真的。
PDD_EVIDENCE_HINT = evidence.dir_hint("pdd")


# --------------------------------------------------------------------------- #
# 调试取证：每一步都能落一份现场
# --------------------------------------------------------------------------- #
#
# 落点统一由 `myUtils/publish_evidence` 决定：`screenshots/pdd/`。原来这里是自己
# 一个 `pdd_debug/` 外层目录，和另外四个平台的 `screenshots/` 分家——同一个发布中心
# 的失败现场散在两处，排查时先得想起"拼多多那个在别的地方"。


def _debug_dir() -> Path:
    """现场目录（`screenshots/pdd/`），顺手建出来。"""
    return evidence.platform_dir("pdd")


async def _dump(page: Page, tag: str) -> str:
    """把当前页面的 HTML + 全页截图落到 `screenshots/pdd/`，返回路径前缀（失败返回空串）。

    **这个函数自己绝不允许把发布搞失败**（和快手那条路同一个教训）：截图前 Playwright
    会等 `document.fonts.ready`，机器忙的时候能一直撞到 30 秒默认超时；取证是有界的副产物，
    不是发布的一环。所以：自己的超时 + 自己的 try，异常只记日志。
    """
    png = await evidence.capture(
        page,
        "pdd",
        tag,
        logger=pdd_logger,
        html=True,
        timeout_ms=PDD_DEBUG_SHOT_TIMEOUT_MS,
    )
    if not png:
        return ""
    prefix = Path(png).with_suffix("")
    pdd_logger.warning(_msg("📸", f"现场文件（url={page.url}）: {prefix}.png / .html"))
    return str(prefix)


async def _dump_if_enabled(page: Page, tag: str) -> None:
    """按开关决定要不要留现场。

    * `PDD_DEBUG_DUMP`（conf.py / `SAU_PDD_DUMP=1`）→ 每一步都留；
    * 环境变量 `SAU_PDD_TRACE=1` → 临时打开，不用改文件。
    """
    if PDD_DEBUG_DUMP or os.environ.get("SAU_PDD_TRACE") == "1":
        await _dump(page, tag)


# --------------------------------------------------------------------------- #
# 页面基础操作
# --------------------------------------------------------------------------- #


def _browser_launch_kwargs() -> dict:
    if LOCAL_CHROME_PATH:
        return {"executable_path": LOCAL_CHROME_PATH}
    return {"channel": "chromium"}


async def _goto_pdd_page(page: Page, url: str, *, what: str = "拼多多页面") -> None:
    """打开拼多多页面：显式 `domcontentloaded` + 90 秒，失败重来一次。

    沿用快手那条路的结论：首次导航用默认的 `load` + 30 秒在"并发开多个浏览器的第一条
    任务"上必然偏紧，而重试没有副作用（那时页面上还什么都没有）。
    """
    last_error: Exception | None = None
    for attempt in range(1, PDD_NAV_ATTEMPTS + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=PDD_NAV_TIMEOUT_MS)
            if attempt > 1:
                pdd_logger.warning(_msg("⚠️", f"{what}第 {attempt} 次导航才成功（已自动恢复）"))
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < PDD_NAV_ATTEMPTS:
                pdd_logger.warning(
                    _msg("😵", f"{what}第 {attempt}/{PDD_NAV_ATTEMPTS} 次导航失败，马上重试: {exc}")
                )
                await asyncio.sleep(2)
    raise RuntimeError(
        f"打开{what}连续 {PDD_NAV_ATTEMPTS} 次导航失败（每次 {PDD_NAV_TIMEOUT_MS / 1000:g} 秒）：{last_error}"
    )


def _is_login_url(url: str) -> bool:
    return PDD_LOGIN_URL_MARKER in url


#: 每个候选选择器最多往后看几个匹配元素去找"可见的那个"。
#: 页面上同名元素可能很多（比如「发布」：导航、隐藏弹窗、真按钮），
#: 但可见的那个一定在前面几个里，看太多只是白花时间。
_PDD_VISIBLE_SCAN_LIMIT = 12


async def _first_visible(
    page: Page,
    selectors: tuple[str, ...],
    *,
    timeout_ms: int = 0,
    scope: Page | Locator | None = None,
) -> Locator | None:
    """按顺序试选择器，返回第一个**可见**的 locator；都没有就返回 None。

    `scope` 用于"在某个容器里找"（比如只在弹窗里找它的「取消」按钮）。

    `timeout_ms > 0` 时会轮询等待（页面渲染慢时用），否则只做一次性检查。

    ⚠️ 这里**不能用 `.first`**：`.first` 是"DOM 顺序里的第一个匹配"，不是"第一个可见的"。
    离线自测（`tools/verify_pdd_publish_flow_offline.py`）第一次跑就把这个坑踩出来了：
    假页面里先有一个**隐藏的**「仍要发布」按钮，真「发布」按钮排在它后面，
    于是 `button:has-text("发布")` 永远命中那个隐藏按钮、`is_visible()` 恒为 False，
    发布停在"找不到发布按钮"上——而页面上明明有一个可见的发布按钮。
    真页面同样会有这个问题（导航、弹窗里到处是带「发布」二字的按钮），所以逐个往后找。
    """
    root = scope if scope is not None else page
    deadline = time.monotonic() + timeout_ms / 1000 if timeout_ms else 0
    while True:
        for selector in selectors:
            try:
                locator = root.locator(selector)
                count = await locator.count()
            except Exception:  # noqa: BLE001 —— 单个选择器语法/超时问题不该中断整轮
                continue
            for index in range(min(count, _PDD_VISIBLE_SCAN_LIMIT)):
                try:
                    candidate = locator.nth(index)
                    if await candidate.is_visible():
                        return candidate
                except Exception:  # noqa: BLE001
                    continue
        if not deadline or time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.5)


async def _first_present(
    page: Page, selectors: tuple[str, ...], *, timeout_ms: int = 0, scope: Page | Locator | None = None
) -> Locator | None:
    """按顺序试选择器，返回第一个**存在于 DOM** 的元素（**不要求可见**）。

    为什么必须和 `_first_visible` 分开：**文件输入框天生是隐藏的**。
    真实页面（2026-10-08 核对）里两个上传 input 都是 `visible=False`
    （`<input type=file accept=".mp4,.wmv,.mov,.avi,.m4v">` 挂在拖拽区上、
    样式藏起来由「添加视频」按钮代替），要求可见就永远找不到——
    这正是首次真实登录时"打开发布页但等不到上传入口"的直接原因。
    """
    root = scope if scope is not None else page
    deadline = time.monotonic() + timeout_ms / 1000 if timeout_ms else 0
    while True:
        for selector in selectors:
            try:
                locator = root.locator(selector)
                if await locator.count():
                    return locator.nth(0)
            except Exception:  # noqa: BLE001
                continue
        if not deadline or time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.5)


async def _upload_page_ready(page: Page) -> bool:
    """当前页面是不是「能上传」的那一页。

    判据（真实页面核对过）：
      * 视频文件输入框在 DOM 里（隐藏也算）——它只在上传页渲染；
      * 或者「添加视频」按钮可见。

    刻意**不**用"输入框可见"：文件输入框是隐藏的（见 `_first_present`）。
    """
    if await _first_present(page, PDD_SELECTORS["file_input"], timeout_ms=0) is not None:
        return True
    return await _first_visible(page, PDD_SELECTORS["upload_button"], timeout_ms=0) is not None


async def _ensure_upload_page(page: Page) -> None:
    """确保停在真正的上传页上；不在就想办法走过去。

    为什么需要这一步：拼多多的「发布视频」有两个页面——
      * `/n-creator/video/short-video`：**活动/任务落地页**，一个输入框都没有；
      * `/n-creator/video/home`：真正的上传页（拖拽区 + 添加视频 + 视频卡片）。
    而且**平台自己会在两者之间跳**（登录后回到哪个页面并不总是我们指定的那个）。
    所以：先看当前页，不行就点落地页上的「立即发布短视频」，再不行就重新导航一次。
    """
    if await _upload_page_ready(page):
        return

    for attempt in range(1, PDD_UPLOAD_ENTRY_ATTEMPTS + 1):
        pdd_logger.warning(
            _msg("🧭", f"当前页不是上传页（第 {attempt} 次尝试），试着点进「发布视频」")
        )
        entry = await _first_visible(
            page,
            (
                'text=立即发布短视频',
                'text=发布视频',
                '[class*="sidebar"] a:has-text("发布视频")',
                '[class*="menu"] :text("发布视频")',
            ),
            timeout_ms=8_000,
        )
        if entry is not None:
            try:
                await entry.click(timeout=5_000)
                await asyncio.sleep(3)
            except Exception as exc:  # noqa: BLE001
                pdd_logger.warning(_msg("⚠️", f"点「发布视频」失败（{str(exc)[:60]}），改用直接导航"))
        if await _upload_page_ready(page):
            pdd_logger.success(_msg("🥳", "已经从落地页进到上传页"))
            return

        # 先回真正的上传页；还不行就去"活动落地页"再点一次「立即发布短视频」
        # （平台有时把人带到落地页，而落地页上确实有这个入口）。
        await _goto_pdd_page(page, PDD_PUBLISH_URL, what="拼多多上传页")
        await asyncio.sleep(2)
        if await _upload_page_ready(page):
            return

        await _goto_pdd_page(page, PDD_PUBLISH_URL_LEGACY, what="拼多多活动落地页")
        await asyncio.sleep(2)
        landing_entry = await _first_visible(page, ('text=立即发布短视频',), timeout_ms=6_000)
        if landing_entry is not None:
            try:
                await landing_entry.click(timeout=5_000)
                await asyncio.sleep(3)
            except Exception as exc:  # noqa: BLE001
                pdd_logger.warning(_msg("⚠️", f"点落地页的「立即发布短视频」失败: {str(exc)[:60]}"))
        if await _upload_page_ready(page):
            return

    await _dump(page, "not-upload-page")
    raise RuntimeError(
        "打开的不是拼多多的上传页（既没有视频上传输入框，也没有「添加视频」按钮）。"
        f"当前地址：{page.url}。现场已存到 {PDD_EVIDENCE_HINT}。"
    )


async def _dismiss_blocking_dialogs(page: Page) -> None:
    """关掉会挡住发布流程的弹窗。

    已知会挡路的两类（文案都取自平台前端产物）：

    1. **草稿残留弹窗**「上次有 N 个视频上传成功但未发布，是否继续发布」——
       一律点它自己的「取消」：那是上一次的半成品，替用户"顺手发出去"是越权，
       而且会让发布记录和平台上实际有的内容对不上；
    2. 纯提示：「我知道了」/「仍要发布」。

    ⚠️ 「取消」这类**通用文案绝不能全局瞎点**：页面上别的地方（合集弹窗的「取消添加」等）
    也有取消按钮，点错一个就可能把用户已经选好的东西清掉。所以第 1 类只在
    **确认弹窗文本存在**之后，才在那个弹窗容器里找按钮。
    """
    for dialog_text in ("是否继续发布", "上传成功但未发布"):
        try:
            scope = await _first_visible(
                page,
                tuple(sel for sel in PDD_SELECTORS["draft_dialog"] if dialog_text in sel),
                timeout_ms=0,
            )
            if scope is None:
                continue
            cancel = await _first_visible(page, ('button:has-text("取消")',), timeout_ms=0, scope=scope)
            if cancel is not None:
                await cancel.click(timeout=3000)
                pdd_logger.info(_msg("🪟", "已关掉「草稿残留、是否继续发布」弹窗（选择不发布）"))
                await asyncio.sleep(0.5)
        except Exception:  # noqa: BLE001 —— 弹窗关不掉不该让发布失败，继续往下走
            continue

    for text in ("我知道了", "仍要发布"):
        try:
            locator = await _first_visible(page, (f'button:has-text("{text}")',), timeout_ms=0)
            if locator is not None:
                await locator.click(timeout=3000)
                pdd_logger.info(_msg("🪟", f"已关闭弹窗：{text}"))
                await asyncio.sleep(0.5)
        except Exception:  # noqa: BLE001
            continue


async def _wait_publish_page_ready(page: Page) -> None:
    """等发布页渲染到"可以开始上传"。"""
    last_error: Exception | None = None
    for attempt in range(1, PDD_UPLOAD_ENTRY_ATTEMPTS + 1):
        if await _first_present(
            page,
            PDD_SELECTORS["file_input"],
            timeout_ms=PDD_UPLOAD_ENTRY_TIMEOUT_MS,
        ) is not None or await _first_visible(
            page, PDD_SELECTORS["upload_button"], timeout_ms=5_000
        ) is not None:
            if attempt > 1:
                pdd_logger.warning(_msg("⚠️", f"上传入口第 {attempt} 次才出现（已自动恢复）"))
            return
        last_error = RuntimeError("等不到上传入口")
        if attempt < PDD_UPLOAD_ENTRY_ATTEMPTS:
            pdd_logger.warning(_msg("😵", "发布页的上传入口没出现，重试一次"))
            await asyncio.sleep(2)

    await _dump(page, "upload-entry-missing")
    raise RuntimeError(
        f"拼多多发布页等不到上传入口（{PDD_UPLOAD_ENTRY_TIMEOUT_MS / 1000:g} 秒 × "
        f"{PDD_UPLOAD_ENTRY_ATTEMPTS} 次），疑似页面改版或登录态异常。"
        f"现场已存到 {PDD_EVIDENCE_HINT}，请据此核对 PDD_SELECTORS。{last_error or ''}"
    )


# --------------------------------------------------------------------------- #
# 各步骤
# --------------------------------------------------------------------------- #


async def _set_video_file(page: Page, file_path: str) -> None:
    """把视频塞进发布页的文件输入框。

    两条路，优先第一条：

    1. **直接 `set_input_files`**（大多数平台的做法，稳、不依赖按钮位置）；
    2. 找不到 input 就点「添加视频」走系统文件选择器（`expect_file_chooser`）。
    """
    # 用 `_first_present`：文件输入框在真实页面上是**隐藏的**（visible=False），
    # 要求可见会永远找不到（见 _first_present 的说明）。
    file_input = await _first_present(page, PDD_SELECTORS["file_input"], timeout_ms=15_000)
    if file_input is not None:
        await file_input.set_input_files(file_path)
        pdd_logger.success(_msg("📤", f"已选择视频文件（input[type=file]）: {Path(file_path).name}"))
        return

    button = await _first_visible(page, PDD_SELECTORS["upload_button"], timeout_ms=10_000)
    if button is None:
        await _dump(page, "no-file-input")
        raise RuntimeError(
            "找不到上传视频的入口（既没有 input[type=file]，也没有「添加视频」按钮）。"
            f"现场已存到 {PDD_EVIDENCE_HINT}。"
        )
    async with page.expect_file_chooser() as fc_info:
        await button.click()
    chooser = await fc_info.value
    await chooser.set_files(file_path)
    pdd_logger.success(_msg("📤", f"已选择视频文件（文件选择器）: {Path(file_path).name}"))


async def _publish_form_ready(page: Page) -> bool:
    """判断"视频卡片已经建好、可以填表了"。

    判据是"只有视频卡片建好之后才会出现的东西"，任一成立即算就绪：

    * 卡片容器 `[class*="video-list_itemWrap"]`——平台自己的类名，最可靠；
    * 描述编辑器 `[class*="saboIpt"]`；
    * 卡片上才有的文案：「立即发布」「定时发布」「请添加封面」「添加商品」。

    上传区（还没选文件时）只有「点击上传…拖拽至此区域」这类文案，不会误判。
    ⚠️ **不要拿"标题输入框"当判据**：拼多多的视频发布没有标题框
    （那个框只在图文时渲染），拿它判断会让每一次上传都等到超时。
    """
    card_markers = (
        # 平台自己的卡片类名（最可靠）
        '[class*="video-list_itemWrap"]',
        # 卡片上才有的控件与文案
        '[class*="saboIpt"]',
        'text=立即发布',
        'text=定时发布',
        'text=请添加封面',
        'text=添加商品',
        # 兜底：万一平台给视频也加了标题框
    ) + PDD_SELECTORS["title_input"]
    return await _first_visible(page, card_markers, timeout_ms=0) is not None


async def _upload_progress_text(page: Page) -> str:
    """取"上传进度区"的可见文案，用来判断有没有进展。

    真实页面里上传进度挂在 `video-list_uploadPercent__*`（完成时是「视频上传成功」）；
    卡片还没建出来时它在拖拽区里。取不到就退化成空串——空串也是有效信息（一直空 = 一直没动静）。
    """
    try:
        return await page.evaluate(
            """() => {
                const el = document.querySelector('[class*="uploadPercent"], [class*="video-list_itemWrap"], [class*="draggerWrap"]');
                return ((el && el.innerText) || '').replace(/\\s+/g, ' ').trim().slice(0, 120);
            }"""
        )
    except Exception:  # noqa: BLE001
        return ""


async def _wait_upload_once(page: Page) -> str:
    """等一次上传完成。返回 "ready" 或 "stalled"；平台报错则直接抛。"""
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + PDD_UPLOAD_STALL_SECONDS
    progress_texts = ("上传中", "上传进度", "正在上传")
    signature = ""
    last_change = loop.time()
    last_log = 0.0
    baseline_toast = await _visible_toast_text(page)

    while loop.time() < deadline:
        try:
            uploading = await _first_visible(
                page, tuple(f"text={text}" for text in progress_texts), timeout_ms=0
            ) is not None
            if not uploading and await _publish_form_ready(page):
                return "ready"

            # 平台自己报的错（例如「视频超出最大时长，暂不支持上传」「上传失败」）
            toast = await _visible_toast_text(page)
            for keyword, hint in UPLOAD_ERROR_HINTS.items():
                if keyword in toast and keyword not in baseline_toast:
                    raise RuntimeError(f"平台拒绝了这次上传：{hint}（弹窗提示含「{keyword}」）")

            current = await _upload_progress_text(page)
            if current != signature:
                signature, last_change = current, loop.time()
            elif loop.time() - last_change > 45:
                # 45 秒进度文案一个字都没变，且卡片也没出来 → 判定卡住
                pdd_logger.warning(
                    _msg("😵", f"上传进度 {45} 秒没有任何变化（当前：{current[:40]!r}），判定卡住")
                )
                return "stalled"

            # 每 30 秒说一句，别让界面看起来像死了
            if loop.time() - last_log > 30:
                last_log = loop.time()
                pdd_logger.info(
                    _msg("⏳", f"还在等平台接收/转码… 已等 {loop.time() - started:.0f} 秒"
                                f"{'（进度：' + current[:40] + '）' if current else ''}")
                )
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 轮询期间的瞬时异常不该判死
            pdd_logger.warning(_msg("⚠️", f"等待上传时的小插曲（继续等）: {exc}"))
        await asyncio.sleep(3)

    return "stalled"


async def _wait_upload_finished(page: Page, file_path: str) -> None:
    """等视频传完（含平台侧转码）；**卡住会自动重传一次**。

    为什么要这套自救（2026-10-09 实测）：一批 22 条里有 3 条卡在"文件已选中、平台侧再没动静"，
    素材本身没问题（4MB / 10~25 秒 / 有音轨），草稿箱里也什么都没有——是平台侧那次上传没响应。
    原来的逻辑是安静地等满 10 分钟，界面上只有转圈，用户只能手动中断。

    现在：超过 `PDD_UPLOAD_STALL_SECONDS` 秒没有任何进展 → 判定卡住 → **重新加载页面、
    重新选文件上传一次**（卡死的上传器/请求靠重载清掉，这是最常见的自救）；再卡住就带着
    明确原因失败，而不是一直转圈。
    """
    for attempt in range(1, PDD_UPLOAD_RETRIES + 2):
        outcome = await _wait_upload_once(page)
        if outcome == "ready":
            if attempt > 1:
                pdd_logger.success(_msg("🥳", f"第 {attempt} 次上传成功（重传救回来了）"))
            pdd_logger.success(_msg("🥳", "视频已经上传完成（发布表单已可编辑）"))
            return

        if attempt > PDD_UPLOAD_RETRIES:
            break
        pdd_logger.warning(
            _msg("🔁", f"平台侧上传卡住了，重新加载页面再传一次（第 {attempt + 1} 次尝试）")
        )
        await _dump(page, "upload-stalled")
        try:
            await page.reload(wait_until="domcontentloaded", timeout=PDD_NAV_TIMEOUT_MS)
            await asyncio.sleep(3)
            await _dismiss_blocking_dialogs(page)
            await _ensure_upload_page(page)
            await _wait_publish_page_ready(page)
            await _set_video_file(page, file_path)
        except Exception as exc:  # noqa: BLE001
            pdd_logger.warning(_msg("⚠️", f"重传前的准备工作出错（继续尝试）: {str(exc)[:80]}"))

    await _dump(page, "upload-timeout")
    raise RuntimeError(
        f"平台侧上传没完成：文件已经选中，但 {PDD_UPLOAD_STALL_SECONDS} 秒内平台始终没有把视频收下"
        f"（已自动重传 {PDD_UPLOAD_RETRIES} 次）。这不是本地文件的问题，多半是平台那一次上传/转码"
        f"没有响应——过一会儿重发即可。现场已存到 {PDD_EVIDENCE_HINT}。"
    )


async def _fill_desc(page: Page, text: str) -> bool:
    """把文案写进描述编辑器，返回是否写成功。

    ⚠️ 这里踩过两个认知坑，都是被平台自己的前端产物纠正的：

    1. **描述不是 textarea**，是平台自带的富文本编辑器（SABO/ACE，`contenteditable`）。
       所以既不能用 `fill()`，也不能只 `type()` 完就完事——得先点进去拿到焦点，
       输入完再确认内容真的落进去了（见下面 `_read_editor_text` 的核对）。
    2. 编辑器里有 **`hackPlaceholder` 占位层**（一句灰字），空的时候它在、
       有内容之后平台自己会把它去掉。所以"有没有写进去"不能靠占位文案判断。
    """
    if not text:
        return True

    editor = await _first_visible(page, PDD_SELECTORS["desc_editor"], timeout_ms=30_000)
    if editor is None:
        await _dump(page, "no-desc-editor")
        raise RuntimeError(
            f"找不到「描述」编辑器（富文本 contenteditable）。现场已存到 {PDD_EVIDENCE_HINT}，"
            f"请据此核对 PDD_SELECTORS 里的 desc_editor。"
        )

    await editor.click()
    await asyncio.sleep(0.3)
    # 清空可能已有内容（编辑草稿的场景），用全选 + 删除，别依赖 fill()
    for key in ("Control+KeyA", "Delete"):
        try:
            await page.keyboard.press(key)
        except Exception:  # noqa: BLE001
            pass
    await asyncio.sleep(0.2)

    # 逐字输入：富文本编辑器对"一次性塞一大段"经常只保留第一段，
    # 而真人也是一句话一句话打的。步长给 0，靠 text 的 delay 控制节奏。
    await page.keyboard.type(text, delay=25)
    await asyncio.sleep(0.5)

    # 输入 `#话题` 会拉起话题联想浮层（businessTopic_*）。不选的话它会盖在编辑器上，
    # 挡住后面的封面/发布按钮；按 Esc 收掉（已输入的文本不受影响）。
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.2)
    except Exception:  # noqa: BLE001
        pass

    ok = await _editor_contains(page, text)
    if ok:
        pdd_logger.success(_msg("✍️", f"描述已填（{len(text)} 字）: {text[:60]}"))
    else:
        pdd_logger.warning(
            _msg("⚠️", f"描述可能没完全写进去（编辑器里读不到完整文本），已存现场")
        )
        await _dump(page, "desc-mismatch")
    return ok


async def _editor_contains(page: Page, text: str) -> bool:
    """核对描述编辑器里真的有这段文本。

    取编辑器可见文本的前 20 个非空白字符做包含判断——**不用全文比对**：
    富文本编辑器会把 `#话题` 变成带样式的一小块（DOM 里多了标签），
    拿原文做全等比对必然对不上；而"前 20 个字符在不在"足够区分
    "写进去了"和"什么都没写进去"。
    """
    try:
        editor_text = await page.evaluate(
            """() => {
                const el = document.querySelector('[class*="saboIpt"], [class*="emptyReactSabo"], [contenteditable="true"]');
                return (el && (el.innerText || el.textContent)) || '';
            }"""
        )
    except Exception:  # noqa: BLE001
        return False
    probe = "".join(text.split())[:20]
    return bool(probe) and probe in "".join(editor_text.split())


async def _fill_title(page: Page, title: str) -> bool:
    """如果有标题输入框就填（图文场景 / 平台改版），返回是否填了。

    **视频发布页上没有标题框**——平台只在图文（feedType=IMAGE）时渲染它。
    所以这里"找不到"是**正常情况**，不是错误：标题会由 `_compose_desc`
    并进描述里（见那个方法的说明）。
    """
    locator = await _first_visible(page, PDD_SELECTORS["title_input"], timeout_ms=0)
    if locator is None:
        pdd_logger.info(
            _msg("ℹ️", "这个页面没有标题输入框（拼多多的视频发布没有标题字段），标题会并进描述")
        )
        return False
    await locator.click()
    await locator.fill("")
    await locator.fill(title)
    pdd_logger.success(_msg("✍️", f"标题已填（页面有标题框）: {title}"))
    return True


def _fit_cover_spec(frame_path: Path, work_dir: Path) -> Path | None:
    """把抽出来的首帧处理成平台能收的封面：**3:4 竖版、宽 ≥720、≤5MB**。

    平台自己的文案是「需上传封面比例为3:4且分辨率大于720P；文件大小不超过5M」。

    两种不合规都是**居中裁**，不拉伸：
      * 比 3:4 更宽（横版素材）→ 裁两侧；
      * 比 3:4 更窄（9:16 竖版素材，多多视频最常见）→ 裁上下。
    拉伸会把画面压变形，而封面是给人看的第一眼；裁掉的部分对竖版平台来说本来就是边缘内容。

    PIL 是仓库里已有的依赖（二维码自检在用），不引入新包。
    任何失败都只返回 None（上层用原始帧继续），绝不把发布拦在本地。
    """
    try:
        from PIL import Image

        with Image.open(frame_path) as image:
            image = image.convert("RGB")
            target = PDD_COVER_RATIO[0] / PDD_COVER_RATIO[1]
            width, height = image.size
            current = width / height
            if current > target + 0.005:
                new_width = max(1, int(height * target))
                left = (width - new_width) // 2
                image = image.crop((left, 0, left + new_width, height))
            elif current < target - 0.005:
                new_height = max(1, int(width / target))
                top = max(0, (height - new_height) // 2)
                image = image.crop((0, top, width, top + new_height))

            if image.width < PDD_COVER_MIN_WIDTH:
                scale = PDD_COVER_MIN_WIDTH / image.width
                image = image.resize(
                    (PDD_COVER_MIN_WIDTH, max(1, round(image.height * scale))),
                    Image.LANCZOS,
                )

            out = work_dir / "cover_3x4.jpg"
            image.save(out, "JPEG", quality=90)
            if out.stat().st_size > PDD_COVER_MAX_MB * 1024 * 1024:
                image.save(out, "JPEG", quality=70)
            pdd_logger.info(
                _msg(
                    "🖼️",
                    f"封面已按平台规格处理: {image.width}×{image.height}"
                    f"（{out.stat().st_size / 1024:.0f} KB）",
                )
            )
            return out
    except Exception as exc:  # noqa: BLE001
        pdd_logger.warning(_msg("⚠️", f"封面规格处理失败，改用原始首帧: {exc}"))
        return None


def _auto_cover_frame(video_path: str) -> str | None:
    """从视频首帧生成一张封面图，返回路径（失败返回 None）。

    为什么需要它：拼多多的封面是**发布硬前提**（少封面页面直接拒绝，文案「请添加封面」），
    而发布中心目前的「封面」选项并没有暴露到界面上（前端固定传空串），所以不能把
    "用户会自己给封面"当前提。首帧恰好就是平台默认封面会取的那一帧，语义上完全等价，
    而且这一步用的 ffmpeg 是仓库里已有的（`imageio-ffmpeg` 自带静态二进制，
    见 `uploader/xiaohongshu_uploader/ai_copywriter.py` 的 `extract_key_frames`）。

    生成结果缓存在 `screenshots/pdd/covers/`（按视频文件名），同一素材重发不用反复抽帧。
    它跟着现场目录走是因为**这个目录归拼多多这条链路专用**、且已在 `.gitignore` 里
    （生成物本来也不该入库）；单独再起一个外层目录只多一个要记住的路径。
    """
    video = Path(video_path)
    out_dir = _debug_dir() / "covers"
    out_dir.mkdir(parents=True, exist_ok=True)
    cached = out_dir / f"{video.stem}.jpg"
    if cached.is_file() and cached.stat().st_size > 0:
        return str(cached)

    # 每调用一次用一个**独立的临时目录**：发布中心是"平台 × 账号"并行跑，
    # 同一个素材可能同时在两三个浏览器里发，而 `extract_key_frames` 固定写
    # `first.jpg`——共用一个目录就会互相覆盖，抽出来的是别人写了一半的图。
    work = out_dir / f"tmp-{uuid.uuid4().hex}"
    try:
        from uploader.xiaohongshu_uploader.ai_copywriter import extract_key_frames

        result = extract_key_frames(video, out_dir=work)
        first = result.get("first")
        if first and Path(first).is_file():
            # 抽出来的首帧是**视频原始比例**，而平台要求封面 3:4 竖版、≥720P、≤5MB
            # （它的文案是「需上传封面比例为3:4且分辨率大于720P；文件大小不超过5M」）。
            # 直接传原比例会被拒，所以这里补一步"按 3:4 裁+补+缩放"。
            composed = _fit_cover_spec(Path(first), work)
            source = composed or Path(first)
            # 先写 .part 再原子替换：并发下别人要么看到完整的旧文件，要么看到完整的新文件
            part = cached.with_name(cached.name + ".part")
            shutil.copyfile(source, part)
            os.replace(part, cached)
            return str(cached)
        pdd_logger.warning(_msg("⚠️", f"抽首帧失败，无法自动生成封面：{result.get('errors')}"))
    except Exception as exc:  # noqa: BLE001 —— 抽帧失败不该让发布直接失败
        pdd_logger.warning(_msg("⚠️", f"抽首帧异常，无法自动生成封面: {exc}"))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return None


def _declaration_options(label: str) -> tuple[str, ...]:
    """把 `declaration_option` 模板里的 `{label}` 填上。"""
    return tuple(sel.replace("{label}", label) for sel in PDD_SELECTORS["declaration_option"])


async def _read_declaration_value(page: Page) -> str:
    """回读「内容声明」当前选中的值。

    ⚠️ 必须读 **input 的 value**：这个下拉是 rc-select，选中的文案渲染在
    `<input data-testid="beast-core-select-htmlInput" value="…">` 里，
    而 **innerText 不包含 input 的 value**。第一次验证时就是栽在这里——
    明明选上了、截图里也显示「内容无需标注」，回读却当没选上。
    """
    try:
        return await page.evaluate(
            """() => {
                const sec = document.querySelector('[class*="ContentDeclaration_statement"]');
                if (!sec) return '';
                const input = sec.querySelector('input');
                if (input && input.value) return input.value;
                return (sec.innerText || '').replace('内容声明', '').trim();
            }"""
        )
    except Exception:  # noqa: BLE001
        return ""


async def _set_content_declaration(page: Page, label: str | None = None) -> bool:
    """选「内容声明」——**拼多多的必选项**，不选就发不出去。

    平台的行为（真实页面核对）：`anchorDeclarationType` 为空时，点「发布」只会弹一句
    `TST_notice` 警告「请选择内容声明」，**不发任何请求**。所以这一步必须做在提交之前。

    选项来自发布页初始化接口的 `anchorDeclarationList`（2026-10-08 实测）：
    内容无需标注(11) / 含AI生成内容(1) / 含虚构演绎内容(5) / 内容含营销信息(7) /
    内容为转载(3) / 个人观点，仅供参考(8)。选哪一项由 `conf.PDD_CONTENT_DECLARATION` 决定，
    默认「内容无需标注」（与抖音「自主声明」、视频号「视频标注」的既有取舍一致）。
    """
    label = label or PDD_CONTENT_DECLARATION
    section = await _first_visible(page, PDD_SELECTORS["declaration_section"], timeout_ms=20_000)
    if section is None:
        await _dump(page, "no-declaration-section")
        pdd_logger.warning(
            _msg("⚠️", "页面上找不到「内容声明」区块——平台可能改版，继续发布（可能被平台拦下）")
        )
        return False

    current = await _read_declaration_value(page)
    if label in (current or ""):
        pdd_logger.info(_msg("ℹ️", f"内容声明已经是「{current}」，跳过"))
        return True

    trigger = await _first_visible(
        page, PDD_SELECTORS["declaration_trigger"], timeout_ms=10_000, scope=section
    )
    if trigger is None:
        pdd_logger.warning(_msg("⚠️", "找不到「内容声明」的下拉框，跳过"))
        return False
    await trigger.click()
    await asyncio.sleep(0.8)

    option = await _first_visible(page, _declaration_options(label), timeout_ms=8_000)
    if option is None:
        await _dump(page, "no-declaration-option")
        pdd_logger.warning(
            _msg("⚠️", f"下拉里没找到「{label}」这一项（选项随账号/规则变化），跳过内容声明")
        )
        try:
            await page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
        return False

    await option.click()
    await asyncio.sleep(0.8)
    # 选项文案带括号说明，选中后回读的可能是「内容无需标注（…）」→ 用包含判断
    selected = label in (await _read_declaration_value(page))
    if selected:
        pdd_logger.success(_msg("✅", f"内容声明已选：{label}"))
    else:
        await _dump(page, "declaration-not-set")
        pdd_logger.warning(_msg("⚠️", f"内容声明点了「{label}」但回读不到，按未选中继续"))
    return selected


async def _card_has_cover(page: Page) -> bool:
    """卡片上是否已经有封面图（真实页面：设置成功后 `[class*="video-list_coverImage"]` 里出现 `<img>`）。"""
    try:
        return bool(
            await page.evaluate(
                """() => {
                    const area = document.querySelector('[class*="video-list_coverImage"]');
                    return !!(area && area.querySelector('img'));
                }"""
            )
        )
    except Exception:  # noqa: BLE001
        return False


async def _set_cover_if_needed(page: Page, cover_path: str | None) -> bool:
    """设置封面，返回是否设置成功。

    **拼多多的封面是发布硬前提**：没有封面时点「发布」，平台只弹一句「请添加封面」
    然后什么都不做（真实页面 2026-10-08 核对；那句话只显示约 2 秒）。所以和「内容声明」
    一样，封面要**在提交前就设好**，不能等平台拒绝。

    真机核对出来的完整流程（`/n-creator/video/home`）：
      1. 点卡片上的「编辑封面」→ 弹出「封面选择」弹窗，默认停在「视频截取」页签；
      2. **必须点「本地上传」页签**，图片文件输入框（`accept=".jpg,.jpeg,.png"`，隐藏）才会出现；
      3. 塞图 → 点「确定」→ 弹窗关闭，卡片的封面区里出现 `<img>` = 设置成功。

    封面图用调用方给的（发布中心 `thumbnail` 选项）；没给就用 `_auto_cover_frame`
    从视频首帧生成一张**符合平台规格（3:4 竖版 / ≥720P / ≤5MB）**的图。
    """
    if await _card_has_cover(page):
        pdd_logger.info(_msg("ℹ️", "卡片上已有封面，跳过"))
        return True
    if not cover_path:
        pdd_logger.warning(_msg("⚠️", "没有可用封面，跳过设置封面（提交时可能被平台拦下）"))
        return False
    try:
        cover_entry = await _first_visible(page, PDD_SELECTORS["cover_entry"], timeout_ms=10_000)
        if cover_entry is None:
            pdd_logger.warning(_msg("⚠️", "找不到封面入口（「编辑封面」），跳过设置封面"))
            return False
        await cover_entry.click()
        await asyncio.sleep(1.5)

        # 关键一步：切到「本地上传」页签，文件输入框才会出现
        tab = await _first_visible(page, PDD_SELECTORS["cover_upload_tab"], timeout_ms=5_000)
        if tab is not None:
            await tab.click()
            await asyncio.sleep(1.0)

        # 文件输入框是隐藏的 → 必须用 _first_present（不能用 _first_visible）
        file_input = await _first_present(page, PDD_SELECTORS["cover_file_input"], timeout_ms=8_000)
        if file_input is None:
            pdd_logger.warning(_msg("⚠️", "封面弹窗里没有图片输入框，跳过设置封面"))
            await _close_cover_modal(page)
            return False

        await file_input.set_input_files(cover_path)
        await asyncio.sleep(2.0)

        confirm = await _first_visible(page, PDD_SELECTORS["cover_confirm"], timeout_ms=8_000)
        if confirm is not None:
            await confirm.click()
            await asyncio.sleep(2.0)

        if await _card_has_cover(page):
            pdd_logger.success(_msg("🖼️", f"封面已设置: {Path(cover_path).name}"))
            return True
        pdd_logger.warning(_msg("⚠️", "点了封面确定但卡片上读不到封面图，按未设置继续"))
        await _dump(page, "cover-not-applied")
        return False
    except Exception as exc:  # noqa: BLE001 —— 封面失败留到提交时由页面兜底报错
        pdd_logger.warning(_msg("⚠️", f"设置封面失败: {exc}"))
        await _dump(page, "cover-failed")
        await _close_cover_modal(page)
        return False


async def _close_cover_modal(page: Page) -> None:
    """把封面弹窗收掉。

    封面这一步"失败"是允许的（页面可能本来就有默认封面），但如果弹窗还开着，
    后面的填标题/点发布全会点到底下的遮罩上——那是把一个小失败放大成整条任务失败。
    """
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
    except Exception:  # noqa: BLE001
        pass


async def _set_schedule(page: Page, publish_date: datetime) -> None:
    """切到「定时发布」并填时间。

    ⚠️ **这一步的选择器最不确定**：定时控件是平台自带的日期时间选择器，
    写代码时拿不到登录态页面，无法确认它的输入格式与交互。所以这里：

    * 先点「定时发布」（这个文案是确定的，来自平台前端产物）；
    * 再按 `placeholder` 找时间输入框，按 `YYYY-MM-DD HH:mm` 输入并回车；
    * 失败了**不静默降级为立即发布**——那会把"定时"变成"现在发出去"，是最坏的结果。
      直接报错并保留现场。
    """
    option = await _first_visible(page, ('text=定时发布', 'label:has-text("定时发布")'), timeout_ms=20_000)
    if option is None:
        await _dump(page, "no-schedule-option")
        raise RuntimeError(
            f"找不到「定时发布」选项，无法按时发布（拒绝降级为立即发布）。现场已存到 {PDD_EVIDENCE_HINT}。"
        )
    await option.click()
    await asyncio.sleep(1)

    time_input = await _first_visible(
        page,
        PDD_SELECTORS["schedule_input"],
        timeout_ms=15_000,
    )
    if time_input is None:
        await _dump(page, "no-schedule-input")
        raise RuntimeError(
            f"点了「定时发布」但找不到时间输入框。现场已存到 {PDD_EVIDENCE_HINT}，请据此核对定时控件。"
        )

    # 格式对齐真机：点「定时发布」后那个输入框自带的值形如 `2026-10-08 16:42:40`（**带秒**），
    # 少写秒有可能被控件判成无效。
    text = publish_date.strftime("%Y-%m-%d %H:%M:%S")
    await time_input.click()
    try:
        await time_input.fill("")
    except Exception as exc:  # noqa: BLE001 —— 有些日期控件不吃 fill()，改用键盘清空
        pdd_logger.warning(_msg("⚠️", f"时间框不支持直接清空（{str(exc)[:40]}…），改用全选删除"))
        try:
            await page.keyboard.press("Control+KeyA")
            await page.keyboard.press("Delete")
        except Exception:  # noqa: BLE001
            pass
    await time_input.type(text, delay=60)
    await asyncio.sleep(0.8)
    try:
        await page.keyboard.press("Enter")
    except Exception:  # noqa: BLE001
        pass
    await asyncio.sleep(0.8)
    await page.keyboard.press("Escape")
    pdd_logger.success(_msg("⏰", f"已切换到定时发布并填入时间: {text}"))
    await _dump_if_enabled(page, "after-schedule")


async def _confirm_publish_dialog(page: Page) -> bool:
    """处理"同意并发布"弹窗：先勾协议，再点确认。返回是否点到了确认按钮。

    真实页面（2026-10-08 核对）：点卡片上的「发布」不会立刻发请求，而是弹一个协议窗：
    左边一个勾选框（`<input type=checkbox>`，实际是 `opacity:0` 的隐藏 input，
    可点的是它旁边的「我已阅读并同意」文案）+ 右边两个按钮「同意并发布」/「不同意」。
    **不勾协议就点确认，什么都不会发生**——这正是第二次真实发布失败的原因：
    当时兜底逻辑找的是「一键发布」，弹窗压根没被点过，于是"点了发布没回执"。
    """
    ok_button = await _first_visible(page, PDD_SELECTORS["publish_confirm"], timeout_ms=0)
    if ok_button is None:
        return False

    # 已勾过就不用再点（重复点会把它取消掉）
    try:
        checked = await page.evaluate(
            """() => {
                const cb = document.querySelector('[data-testid="beast-core-modal-ok-button"]')
                    ?.closest('[class*="MDL_"]')?.querySelector('input[type="checkbox"]');
                return cb ? !!cb.checked : null;
            }"""
        )
    except Exception:  # noqa: BLE001
        checked = None

    if checked is not True:
        consent = await _first_visible(page, PDD_SELECTORS["publish_consent"], timeout_ms=3000)
        if consent is not None:
            try:
                await consent.click(timeout=3000)
                await asyncio.sleep(0.4)
            except Exception as exc:  # noqa: BLE001
                # 真机上出现过"文案点不动"（超时）——勾选框本体是 `opacity:0` 的隐藏 input，
                # 用 JS 直接把 label 点一下，比等 Playwright 的可点性检查靠谱。
                pdd_logger.warning(
                    _msg("⚠️", f"协议文案点不动（{str(exc)[:50]}），改用 JS 点勾选框")
                )
                try:
                    await page.evaluate(
                        """() => {
                            const cb = document.querySelector('[data-testid="beast-core-modal-ok-button"]')
                                ?.closest('[class*="MDL_"]')?.querySelector('input[type="checkbox"]');
                            if (cb && !cb.checked) {
                                cb.click();
                                cb.dispatchEvent(new Event('change', {bubbles: true}));
                            }
                        }"""
                    )
                    await asyncio.sleep(0.4)
                except Exception as exc2:  # noqa: BLE001
                    pdd_logger.warning(_msg("⚠️", f"JS 点勾选框也失败（继续点确认）: {str(exc2)[:50]}"))

    try:
        await ok_button.click(timeout=5000)
    except Exception as exc:  # noqa: BLE001
        pdd_logger.warning(_msg("⚠️", f"点「同意并发布」失败: {str(exc)[:60]}"))
        return False
    pdd_logger.info(_msg("🪟", "已勾选协议并点「同意并发布」"))
    return True


async def _visible_text(page: Page) -> str:
    """页面上**真正渲染出来的文本**（`innerText`）。

    ⚠️ 绝不能用 `page.content()` 做文本判定：那是整份 HTML 源码，**包含 <script> 里的字符串常量**。
    拼多多的发布页是打包过的前端产物，"发布成功""请添加封面"这类文案本来就躺在它的 JS 里——
    拿源码去匹配等于每次都能匹配上，结果就是**永远报成功**。
    （离线自测第一次跑就把这个坑照出来了：假页面里那句 `body.innerHTML = '发布成功'`
    让"被平台拒稿"的场景被判成了成功。）
    `innerText` 只取渲染出来的文本，脚本/样式里的内容不算。
    """
    try:
        return await page.evaluate("() => (document.body && document.body.innerText) || ''")
    except Exception:  # noqa: BLE001 —— 取不到文本就返回空串：调用方"看不见关键字"= 不下结论
        return ""


async def _visible_toast_text(page: Page) -> str:
    """收集页面上"提示类"元素的可见文本。

    为什么要单独收这一份：平台的拒稿原因是**弹出来的**（toast / message / alert），
    而其中有些文案（「请添加封面」）在表单上**本来就有一份静态的**——
    如果拿整页文本去匹配，一次完全正常的发布会因为页面上那句静态文案被判成"平台拒绝"。
    """
    try:
        return await page.evaluate(
            """() => {
                // ⚠️ `TST_notice*` 是拼多多自己的提示层类名（真实页面核对：
                //    「请选择内容声明」弹的就是 `TST_notice_5-180-0 TST_noticeWarn_5-180-0`）。
                //    少了它，平台拦下发布时我们一个字都看不到——首次真实发布两次失败
                //    就是栽在这上面：平台上只弹了一句警告，我们却报"点了发布没回执"。
                const sels = ['[class*="TST_notice"]','[class*="TST_outerWrapper"]',
                              '[class*="toast"]','[class*="Toast"]','[class*="message"]','[class*="Message"]',
                              '[class*="notification"]','[class*="Notification"]','[class*="alert"]','[class*="Alert"]',
                              '[class*="tip"]','[class*="Tip"]','[role="alert"]'];
                const out = [];
                for (const s of sels) {
                    document.querySelectorAll(s).forEach((el) => {
                        const r = el.getBoundingClientRect();
                        const visible = !!(r.width || r.height) && getComputedStyle(el).visibility !== 'hidden';
                        const text = (el.innerText || '').trim();
                        // 只收"像提示"的元素：太长的多半是匹配到了外层容器
                        // （实测 `TST_outerWrapper` 一度把整个页面标题都收进来了）。
                        if (visible && text && text.length <= 200) out.push(text);
                    });
                }
                return out.join(' | ');
            }"""
        )
    except Exception:  # noqa: BLE001 —— 收不到提示文本不该让发布失败，后面还有别的判据
        return ""


class PddPublishRejected(RuntimeError):
    """平台明确拒绝了这次发布（本地校验拦下，或发布接口回了错）。

    单独一个类型是为了让上层能按 `code` 决定"要不要补救一下再试"：
    目前只有 `missing_cover`（平台说没封面）是值得补救的——补一张封面再发一次，
    而"标题超长""话题超限"这类必须人改了再来，重试只会白跑一遍。
    """

    def __init__(self, message: str, code: str = "rejected"):
        super().__init__(message)
        self.code = code


def _brief_json(body, limit: int = 200) -> str:
    """把响应体压成一行短文本，只用于日志（排障时能直接看到平台回了什么）。"""
    try:
        import json as _json

        text = _json.dumps(body, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        text = str(body)
    return text[:limit]


def _publish_response_error(body) -> str:
    """从发布接口的响应体里读出**真正的**错误信息（没有错就返回空串）。

    拼多多的响应信封是 `{success, errorCode, errorMsg, result}`，
    **成功时 `errorCode` 也是非空的**（`1000000`，见 `PDD_RESPONSE_OK_CODES`）。
    所以判读顺序必须是：

      1. `success === False` → 失败；
      2. 错误文案字段（errorMsg 等）非空 → 失败，直接把平台原话带出去；
      3. `errorCode` 存在、**且不在成功码白名单里**、且 `success` 不是明确的 true → 失败；
      4. 其余 → 成功。

    血泪教训：早先写的是"有 errorCode 就算失败"，于是把两次**已经发布成功**的任务记成了失败
    （平台上作品都在，发布记录却写"失败"）。判错方向的代价是不对称的，所以宁可保守。
    """
    if not isinstance(body, dict):
        return ""
    for key in ("success", "isSuccess", "ok"):
        if body.get(key) is False:
            for message_key in ("errorMsg", "error_msg", "errorMessage", "message", "msg"):
                value = body.get(message_key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            code = body.get("errorCode") or body.get("error_code")
            return f"接口返回失败（错误码 {code}）" if code else "接口返回失败"

    for key in ("errorMsg", "error_msg", "errorMessage", "message", "msg"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    code = body.get("errorCode") or body.get("error_code")
    if code is not None and str(code) not in PDD_RESPONSE_OK_CODES:
        if body.get("success") is not True:
            return f"错误码 {code}"
    return ""


async def _submit(page: Page) -> None:
    """点发布，并判定结果。

    判据按可信度排序（这是被平台代码纠正过的设计）：

    1. **发布接口的回执**——点发布后浏览器一定会打
       `POST /api/backbone/video/publish`（路径就写死在它的前端里）。这是唯一
       一手证据：响应里没有错误字段就算发布成功。
    2. **卡片状态**——成功时平台把视频卡片标成已发布（按钮文案从「发布」变
       「已发布」，卡片上盖一层 mask，类名 `video-list_success` / `video-list_mask`）。
    3. **toast**——失败才弹（平台的错误提示走 toast）。

    ⚠️ 为什么不再用"页面出现『发布成功』"：平台**根本没有这句文案**——
    它的发布成功只是改状态 + 打一个内部事件（`singlePublishVideoSuccess`）。
    代码里唯一一处 "发布成功" 是条日志 tag。原来那条判据在真页面上永远不成立，
    会把每一次成功都判成"结论未知"。
    """
    button = await _first_visible(page, PDD_SELECTORS["submit_button"], timeout_ms=30_000)
    if button is None:
        await _dump(page, "no-submit-button")
        raise RuntimeError(f"找不到「发布」按钮。现场已存到 {PDD_EVIDENCE_HINT}。")

    # 可见 ≠ 可点：平台收下视频后还要"处理/转码"一会儿，这期间按钮是禁用态的。
    # 显式等它变可点（而不是让 click() 用默认的 30 秒去撞），等不到就把原因说清楚——
    # 失败信息里必须能看出"是平台还在处理"，而不是一串 element is not enabled。
    #
    # 轮询而不是 `expect(...).to_be_enabled()`：后者只认真实 Locator，离线自测用的假元素
    # 喂不进去（`ValueError: Unsupported type`），会把一批本来在验"点发布之后怎么判读"
    # 的用例一起打掉。这里只要能问出 is_disabled 就行。
    ready_deadline = time.monotonic() + PDD_SUBMIT_READY_TIMEOUT_MS / 1000
    while True:
        try:
            disabled = await button.is_disabled()
        except Exception:  # noqa: BLE001
            # 问不出状态（元素已经没了，或自测里的假元素）：不在这里卡着，交给 click 去报
            disabled = False
        if not disabled:
            break
        if time.monotonic() >= ready_deadline:
            await _dump(page, "submit-disabled")
            raise RuntimeError(
                f"「发布」按钮 {PDD_SUBMIT_READY_TIMEOUT_MS / 1000:.0f} 秒内一直不可点"
                f"（平台还没处理完这条视频，或这一条被平台卡住了）。现场已存到 {PDD_EVIDENCE_HINT}。"
            )
        await asyncio.sleep(1)

    # 先装监听再点：发布接口可能很快返回，先点后装会漏掉。
    responses: list = []

    def _on_response(response) -> None:
        try:
            if PDD_PUBLISH_API_PATH in response.url and response.request.method == "POST":
                responses.append(response)
        except Exception:  # noqa: BLE001
            pass

    page.on("response", _on_response)
    baseline = await _visible_text(page)
    # 提示层的基线：**只认点发布之后新出现的提示**。
    # 为什么要这一份：补封面重试会调用第二次 `_submit`，而上一次那句「请添加封面」
    # 可能还在提示层里没消失——不排掉就会把"上一次的旧提示"当成"这次又被拒"，
    # 于是重试永远失败（离线自测场景⑦当场抓到的）。
    baseline_toast = await _visible_toast_text(page)
    try:
        await button.click()
        pdd_logger.info(_msg("🚀", "已点击发布，等待平台回执"))

        known_errors = {
            "请添加封面": ("missing_cover", "平台要求必须选封面，已尝试用视频首帧自动补一张；若仍失败请在发布中心为该素材指定「封面」"),
            "请将全部视频添加封面": ("missing_cover", "平台要求每条视频都有封面，已尝试用视频首帧自动补一张"),
            "标题最多不超过": ("rejected", f"标题超过 {PDD_TITLE_MAX} 字，请缩短后重试"),
            "标题不允许添加话题": ("rejected", "标题里不能带 # 话题，请把话题移到描述里"),
            "描述超出字数限制": ("rejected", f"描述超过 {PDD_DESC_MAX} 字，请缩短后重试"),
            "最多可以添加5个相关话题": ("rejected", f"话题最多 {PDD_TOPIC_MAX_COUNT} 个，请减少后重试"),
            "单个话题最长15字": ("rejected", f"单个话题不能超过 {PDD_TOPIC_MAX} 字，请缩短后重试"),
            "当前无可发布的视频": ("rejected", "平台上没有待发布的视频（可能上传失败），请重试"),
            "请检查是否有图片未上传成功": ("rejected", "有图片没传完，请重试"),
            "获取视频信息不完整": ("rejected", "平台没拿到完整的视频信息（上传未完成或转码失败），请重试"),
            "请选择内容声明": ("rejected", "平台要求先选「内容声明」（必选项），本工具会自动选「"
                                          + PDD_CONTENT_DECLARATION + "」；若仍报错请把日志给我"),
            "服务器开小差了": ("rejected", "平台服务端报错，请稍后重试"),
        }

        # 冷静期：给平台时间校验 / 弹提示（本地校验失败时不会打接口，只能靠 toast 发现）。
        await asyncio.sleep(PDD_SUBMIT_GRACE_SECONDS)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + PDD_SUBMIT_TIMEOUT_MS / 1000
        dialog_attempts = 0
        while loop.time() < deadline:
            # ① 接口回执
            while responses:
                response = responses.pop(0)
                body = None
                try:
                    body = await response.json()
                except Exception:  # noqa: BLE001 —— 非 JSON 响应只看 HTTP 状态
                    body = None
                error = _publish_response_error(body)
                if error:
                    await _dump(page, "submit-rejected")
                    code = "missing_cover" if "封面" in error else "rejected"
                    raise PddPublishRejected(f"平台拒绝了这次发布：{error}", code=code)
                if response.status >= 400:
                    await _dump(page, "submit-rejected")
                    raise PddPublishRejected(
                        f"发布接口返回 HTTP {response.status}，平台侧没有受理这次发布",
                        code="rejected",
                    )
                pdd_logger.success(
                    _msg("🥳", f"发布接口已回执（HTTP {response.status}），判定为发布成功"
                                f"{'：' + _brief_json(body) if body is not None else ''}")
                )
                return

            # ② 失败提示（toast，或点发布之后才出现的新文本）
            toast = await _visible_toast_text(page)
            content = await _visible_text(page)
            for keyword, (code, hint) in known_errors.items():
                fresh_toast = keyword in toast and keyword not in baseline_toast
                if fresh_toast or (keyword in content and keyword not in baseline):
                    await _dump(page, "submit-rejected")
                    where = "弹窗提示" if fresh_toast else "页面上新出现的提示"
                    raise PddPublishRejected(
                        f"平台拒绝了这次发布：{hint}（{where}含「{keyword}」）", code=code
                    )

            # ③ 卡片状态（兜底，接口被代理吞掉时也能有结论）
            published = await _first_visible(page, PDD_SELECTORS["item_published"], timeout_ms=0)
            if published is not None:
                pdd_logger.success(_msg("🥳", "视频卡片已进入已发布状态，判定为发布成功"))
                return

            # ④ 协议确认弹窗：不勾协议就点确认，平台什么都不会做
            if dialog_attempts < 2:
                if await _confirm_publish_dialog(page):
                    dialog_attempts += 1

            await asyncio.sleep(1.5)

        await _dump(page, "submit-timeout")
        raise RuntimeError(
            "点了发布但等不到任何回执（接口没响、卡片状态没变、也没有错误提示），结论未知——"
            f"请到拼多多创作后台用「内容管理」核对是否已发布。现场已存到 {PDD_EVIDENCE_HINT}。"
        )
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 登录态校验
# --------------------------------------------------------------------------- #


async def cookie_auth(account_file) -> bool:
    """校验拼多多达人登录态。

    判据按真实页面（2026-10-08 用真实登录态核对）重写过：

    1. **被踹到 /login** → 失效。这一条不变，实测确认过跳转 URL 形如
       `/login?isNewCreatorFrom=…&referUrl=…`；
    2. **能停在上传页** → 有效。判据是"视频文件输入框在 DOM 里（隐藏也算）或「添加视频」按钮可见"。

    ⚠️ 这里踩过一个真坑（首次真实登录就是被它误判成"添加失败"的）：
    原来要求"上传入口**可见**"，而文件输入框天生是隐藏的；再加上当时用的地址是
    `/n-creator/video/short-video`（**活动落地页，一个输入框都没有**），
    于是扫码明明成功了，却判定"打开发布页但等不到上传入口，按失效处理"。
    现在地址改成真正的上传页 `/n-creator/video/home`，判据也不再要求可见。
    """
    account_file = str(account_file)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=PDD_HEADLESS, **_browser_launch_kwargs())
        try:
            context = await browser.new_context(storage_state=account_file)
            context = await set_init_script(context)
            page = await context.new_page()
            await _goto_pdd_page(page, PDD_PUBLISH_URL, what="拼多多上传页（登录态校验）")
            await page.wait_for_timeout(3000)
            if _is_login_url(page.url):
                pdd_logger.info(_msg("🥹", "cookie 已失效（跳到登录页）"))
                return False

            # 先给页面一点时间渲染上传区（这个后台是重前端，冷启动要几秒）
            for _ in range(6):
                if await _upload_page_ready(page):
                    pdd_logger.success(_msg("🥳", "cookie 有效（上传页已就绪）"))
                    return True
                await asyncio.sleep(3)

            # 还没就绪：可能落在落地页上，试着走进上传页再判一次
            try:
                await _ensure_upload_page(page)
                pdd_logger.success(_msg("🥳", "cookie 有效（从落地页进入上传页成功）"))
                return True
            except Exception as exc:  # noqa: BLE001
                pdd_logger.warning(_msg("😵", f"进上传页失败，按失效处理: {str(exc)[:120]}"))
                return False
        except Exception as exc:  # noqa: BLE001
            pdd_logger.warning(_msg("😵", f"cookie 校验时出错，按失效处理: {exc}"))
            return False
        finally:
            await browser.close()


# --------------------------------------------------------------------------- #
# 发布
# --------------------------------------------------------------------------- #


class PddVideo(BaseVideoUploader):
    """拼多多视频（达人）发布器。

    参数形状对齐其余四个平台，`sau_backend_publish._invoke_uploader` 直接构造即可：

        PddVideo(title, file_path, tags, publish_date, account_file, desc=..., publish_strategy=...)
    """

    def __init__(
        self,
        title: str,
        file_path: str,
        tags: list[str],
        publish_date: datetime | int,
        account_file: str,
        publish_strategy: str = PDD_PUBLISH_STRATEGY_IMMEDIATE,
        desc: str = "",
        thumbnail_path: str | None = None,
        headless: bool | None = None,
    ):
        self.title = (title or "").strip()
        self.file_path = file_path
        self.tags = [str(tag).strip().lstrip("#") for tag in (tags or []) if str(tag).strip()]
        self.publish_date = publish_date
        self.account_file = str(account_file)
        self.publish_strategy = publish_strategy
        self.desc = (desc or "").strip()
        self.thumbnail_path = thumbnail_path or None
        self.headless = PDD_HEADLESS if headless is None else headless
        self.local_executable_path = LOCAL_CHROME_PATH or None
        self.step_results: list[dict] = []

    # -- 参数校验 ---------------------------------------------------------- #

    def _record(self, step: str, status: str, message: str) -> None:
        """记一条逐步结果。

        形状**刻意对齐**小红书的页面操作层（`uploader/xiaohongshu_uploader/page_ops.py`
        的 `to_dict()`：`step` / `status` / `message`），这样发布中心那套"执行步骤"
        渲染（`PublishCenter.vue` 读 `s.status` 上色）不用为拼多多开第二条分支。
        """
        self.step_results.append(
            {"step": step, "status": status, "message": message, "detail": ""}
        )

    def _build_title(self) -> str:
        """标题：≤ 40 字、且不能带话题（平台对**图文**标题的硬约束）。

        ⚠️ 视频发布**没有标题字段**，所以正常路径下这个方法的结果会被并进描述
        （见 `_compose_desc`）；它存在的意义是"平台哪天给视频也加标题框"时能直接用。
        """
        title = self.title.replace("#", "").strip()
        if len(title) > PDD_TITLE_MAX:
            pdd_logger.warning(
                _msg("✂️", f"标题 {len(title)} 字超过平台上限 {PDD_TITLE_MAX}，已自动截断")
            )
            title = title[:PDD_TITLE_MAX]
        return title

    def _clean_topics(self) -> list[str]:
        """筛出平台能接受的话题：单个 ≤15 字，最多 5 个（平台提示「最多可以添加5个相关话题」）。"""
        topics: list[str] = []
        for tag in self.tags:
            topic = str(tag).strip().lstrip("#")
            if not topic:
                continue
            if len(topic) > PDD_TOPIC_MAX:
                pdd_logger.warning(
                    _msg("✂️", f"话题「{topic}」超过 {PDD_TOPIC_MAX} 字，已跳过（平台不接受）")
                )
                continue
            if len(topics) >= PDD_TOPIC_MAX_COUNT:
                pdd_logger.warning(
                    _msg(
                        "✂️",
                        f"话题超过 {PDD_TOPIC_MAX_COUNT} 个，只保留前 {PDD_TOPIC_MAX_COUNT} 个"
                        f"（平台限制），其余已跳过",
                    )
                )
                break
            topics.append(topic)
        return topics

    def _compose_desc(self, *, title_in_desc: bool = True) -> str:
        """拼出要写进描述编辑器的文本。

        为什么标题要并进描述：**拼多多的视频发布没有标题字段**——
        平台发布载荷里 `title` 只出现在图文（IMAGE）那一支，视频那一支只有
        `desc / cover / vid / scheduleTime / goodsIdList…`；发布页上那个
        placeholder=「添加标题」的输入框，渲染条件也是 `feedType === IMAGE`
        （都可以在它自己的前端产物里核对到）。
        所以发布中心里用户填的「标题」在视频这条路上必须落到描述的第一行，
        否则那段文字就凭空消失了——比报错更糟，因为没人会发现。

        顺序：标题 → 正文 → `#话题`，用换行分隔；总长截到 500 字。
        """
        parts: list[str] = []
        if title_in_desc:
            title = self._build_title()
            if title:
                parts.append(title)
        if self.desc:
            parts.append(self.desc)
        topics = self._clean_topics()
        if topics:
            parts.append(" ".join(f"#{topic}" for topic in topics))

        text = "\n".join(part for part in parts if part).strip()
        if len(text) > PDD_DESC_MAX:
            pdd_logger.warning(
                _msg("✂️", f"描述 {len(text)} 字超过平台上限 {PDD_DESC_MAX}，已自动截断")
            )
            text = text[:PDD_DESC_MAX]
        return text

    def validate_upload_args(self) -> None:
        if not self.title:
            raise ValueError("拼多多视频发布时，title 是必须的")
        self.file_path = str(self.validate_video_file(self.file_path))
        if self.thumbnail_path:
            self.thumbnail_path = str(self.validate_image_file(self.thumbnail_path))
        self.validate_publish_date(self.publish_date)
        if self.publish_strategy == PDD_PUBLISH_STRATEGY_SCHEDULED and self.publish_date in (None, 0):
            raise ValueError("定时发布必须给 publish_date")
        if not Path(self.account_file).is_file():
            raise FileNotFoundError(f"cookie 文件不存在，请先到「账号管理」登录拼多多视频: {self.account_file}")

    # -- 发布 -------------------------------------------------------------- #

    async def _publish(self, playwright: Playwright) -> None:
        self.validate_upload_args()
        title = self._build_title()
        scheduled = self.publish_strategy == PDD_PUBLISH_STRATEGY_SCHEDULED and self.publish_date not in (None, 0)

        pdd_logger.info(
            _msg("🧍", f"准备发布到拼多多视频：{title}（{'定时 ' + self.publish_date.strftime('%Y-%m-%d %H:%M') if scheduled else '立即'}）")
        )
        # 平台自己的素材建议（mp4 / 1080P / 帧率码率…）。原来它是 conf 里一个没人读的常量，
        # 打出来更有用：出问题时能第一时间对照"是不是素材本身不合平台口味"。
        pdd_logger.info(_msg("ℹ️", f"平台素材建议：{PDD_VIDEO_HINT}"))

        browser = await playwright.chromium.launch(headless=self.headless, **_browser_launch_kwargs())
        context = await browser.new_context(storage_state=self.account_file)
        context = await set_init_script(context)
        published = False
        try:
            page = await context.new_page()
            await _goto_pdd_page(page, PDD_PUBLISH_URL, what="拼多多上传页")
            if _is_login_url(page.url):
                raise RuntimeError(
                    "拼多多登录态已失效（上传页被跳到了登录页），请到「账号管理」重新扫码登录拼多多视频后再发"
                )

            await _dismiss_blocking_dialogs(page)
            # 被平台带到活动落地页时自己走进上传页（两个页面长得完全不同）
            await _ensure_upload_page(page)
            await _wait_publish_page_ready(page)
            await _dump_if_enabled(page, "01-ready")

            pdd_logger.info(_msg("📤", "开始上传视频"))
            await _set_video_file(page, self.file_path)
            await _wait_upload_finished(page, self.file_path)
            self._record("upload_video", "ok", "视频上传完成")
            await _dump_if_enabled(page, "02-uploaded")

            # 文案：**视频发布没有标题字段**，所以标题默认并进描述的第一行；
            # 只有页面真的渲染了标题框（图文场景 / 平台改版）时才单独填。
            pdd_logger.info(_msg("✍️", "填写文案（描述编辑器）"))
            titled = await _fill_title(page, title)
            text = self._compose_desc(title_in_desc=not titled)
            if not await _fill_desc(page, text):
                # 文案没写进去就别发了：平台上会留下一条没有描述的空白视频，
                # 比"这次失败"更难收拾。
                raise RuntimeError(
                    "描述没有写进发布页编辑器（内容对不上），已停止发布。"
                    f"现场已存到 {PDD_EVIDENCE_HINT}，请把该目录给我核对编辑器选择器。"
                )
            self._record(
                "fill_form", "ok",
                f"文案 {len(text)} 字（{'标题单独填' if titled else '标题并进描述'}）",
            )

            # 内容声明：平台的**必选项**，不选则点发布只会弹警告、不发请求。
            await _set_content_declaration(page)
            self._record("set_declaration", "ok", f"内容声明：{PDD_CONTENT_DECLARATION}")

            # 封面：**发布硬前提**，同样在提交前设好。
            # 用户给了封面就用他的；没给就抽视频首帧生成一张符合平台规格的（3:4 / ≥720P / ≤5MB）。
            # 为什么不再"等平台说缺封面再补"：平台那句提示只显示约 2 秒，靠事后补救太脆；
            # 而且真机上平台的"自动生成封面"并不是每次都生效。
            cover_path = self.thumbnail_path or _auto_cover_frame(self.file_path)
            cover_ok = await _set_cover_if_needed(page, cover_path)
            self._record(
                "set_cover",
                "ok" if cover_ok else "skipped",
                f"封面：{Path(cover_path).name}" if cover_ok and cover_path else "封面未设置（提交时可能被平台拦下）",
            )
            await _dump_if_enabled(page, "03-form-filled")

            if scheduled:
                pdd_logger.info(_msg("⏰", "切换到定时发布"))
                await _set_schedule(page, self.publish_date)
                self._record("set_schedule", "ok", f"定时 {self.publish_date.strftime('%Y-%m-%d %H:%M')}")

            await _dismiss_blocking_dialogs(page)
            try:
                await _submit(page)
            except PddPublishRejected as exc:
                # 唯一值得补救的一种拒绝：平台说没封面。用视频首帧生成一张合规封面补上，
                # 然后**只重试一次**——本地校验失败时平台根本没收到东西，重试没有重复发布风险。
                if exc.code != "missing_cover":
                    raise
                pdd_logger.warning(_msg("🖼️", "平台说缺封面，用视频首帧生成一张合规封面后重试一次"))
                cover = _auto_cover_frame(self.file_path)
                if not cover:
                    raise
                await _set_cover_if_needed(page, cover)
                await _dismiss_blocking_dialogs(page)
                await _submit(page)
                self._record("cover_retry", "ok", "补封面后重试发布成功")
            self._record("submit", "ok", "平台已确认发布")
            published = True
        except Exception as exc:  # noqa: BLE001 —— 统一留现场再抛
            self._record("failed", "failed", str(exc))
            try:
                page = context.pages[-1] if context.pages else None
                if page is not None:
                    # 标签带上素材名，和另外四个平台一致：同平台多账号并行跑时，
                    # 光看 `-failed` 分不出这张图是哪条素材的。
                    await _dump(page, f"{Path(self.file_path).stem}-publish-failed")
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            if published:
                # 登录态是服务端会话，用完必须回写（平台可能轮换了票据）
                try:
                    await context.storage_state(path=self.account_file)
                    pdd_logger.success(_msg("🥳", "cookie 已回写"))
                except Exception as exc:  # noqa: BLE001
                    pdd_logger.warning(_msg("⚠️", f"cookie 回写失败（不影响发布结论）: {exc}"))
            await context.close()
            await browser.close()

    async def main(self) -> None:
        async with async_playwright() as playwright:
            await self._publish(playwright)


__all__ = [
    "PDD_PUBLISH_STRATEGY_IMMEDIATE",
    "PDD_PUBLISH_STRATEGY_SCHEDULED",
    "PddPublishRejected",
    "PddVideo",
    "cookie_auth",
]
