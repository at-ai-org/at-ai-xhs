# -*- coding: utf-8 -*-
"""小红书创作服务平台 —— 页面契约（选择器）注册表。

为什么要有这个文件
------------------
浏览器自动化最大的维护成本不是逻辑，而是**页面改版**。把选择器散落在业务代码里，
每次改版都要通读全文；集中到一处后，改版时只需要改这个文件，并且可以用
`page_ops.selftest()` 一次性验证**所有**契约是否还在。

约定
----
- 每个契约给 **多个候选选择器**，按优先级从上到下尝试，第一个命中的生效。
  小红书同一个控件经常同时存在新旧两套实现（例如发布按钮的 `xhs-publish-btn`
  与旧版 `.publish-page-publish-btn button.bg-red`），多候选是应对改版的唯一有效手段。
- `required=False` 的契约缺失时不算失败：这些是账号权益相关的能力
  （合集、原创声明、商品绑定），没开通的账号本来就没有对应控件。

改版应急流程
------------
1. 跑 `selftest`，看哪个契约 miss；
2. 用 `--debug`/有头模式打开浏览器，失败时会在 `screenshots/` 留全页截图；
3. 只改本文件里对应的 candidates；
4. 再跑一次 selftest 确认。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


# --------------------------------------------------------------------------- #
# 页面入口
# --------------------------------------------------------------------------- #

CREATOR_BASE_URL_ENV = "SAU_XHS_CREATOR_BASE_URL"
CREATOR_DEFAULT_BASE_URL = "https://creator.xiaohongshu.com"

# 发布页（视频模式）
PUBLISH_VIDEO_PATH = "/publish/publish?from=homepage&target=video"
# 发布页（默认入口，进入后再点 Tab）
PUBLISH_ENTRY_PATH = "/publish/publish"
# 发布成功后平台的跳转地址
PUBLISH_SUCCESS_URL_PATTERN = "**/publish/success?**"
# 笔记管理页（发布后校验用；多个候选，第一个能打开且能读到笔记列表的生效）
NOTE_MANAGER_PATHS: tuple[str, ...] = (
    "/new/note-manager",
    "/note-manager",
    "/publish/note-manager",
)


# --------------------------------------------------------------------------- #
# 契约定义
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Contract:
    """一条页面契约：某个控件"长什么样"。

    Attributes:
        name: 契约名，会出现在日志和 selftest 报告里。
        page: 所属页面，便于分组阅读。
        candidates: 候选选择器（Playwright 语法），按优先级排列。
        required: 缺失时是否算失败。账号权益类控件应为 False。
        note: 备注，写清楚这条契约的坑。
    """

    name: str
    page: str
    candidates: tuple[str, ...]
    required: bool = True
    note: str = ""
    # 是否要求元素"可见"才算命中。
    # 文件上传框是隐藏元素（靠点击拖拽区触发），必须设为 False——
    # 否则自检探不到它，发布流程的 upload_video 也会失败。
    require_visible: bool = True


# ------------------------------- 登录态 ------------------------------------ #

LOGIN = (
    Contract(
        name="login_box",
        page="login",
        candidates=("div[class*='login-box']", ".login-box-container"),
        note="出现即代表未登录；已登录时本就不存在，因此不算必填",
        required=False,
    ),
    Contract(
        name="login_qrcode_switch",
        page="login",
        candidates=("img.css-wemwzq", "div[class*='login-box'] img[class*='css-']"),
        required=False,
        note="切换到「扫一扫」的图标，部分账号默认已在扫码页",
    ),
    Contract(
        name="home_user_avatar",
        page="home",
        candidates=(
            "div[class*='user-info'] img",
            "img[class*='avatar']",
            "div[class*='header'] img[class*='avatar']",
        ),
        required=False,
        note="已登录的标志之一；拿不到也不算失败，以 URL 是否被重定向为准",
    ),
)

# ------------------------------- 发布页 ------------------------------------ #

PUBLISH = (
    Contract(
        name="publish_tab_video",
        page="publish",
        candidates=(
            "div.creator-tab:has-text('上传视频')",
            "div[class*='creator-tab']:has-text('上传视频')",
            "text=上传视频",
        ),
        note="切换 Tab；上传视频后页面会换成编辑器，Tab 行消失，所以不算必填",
        required=False,
    ),
    Contract(
        name="upload_input",
        page="publish",
        candidates=(
            "div[class^='upload-content'] input.upload-input",
            "input.upload-input",
            "input[type='file'][accept*='video']",
            "input[type='file']",
        ),
        note="set_input_files 的目标；上传视频必须用本地绝对路径",
        # 实测：该 input 是隐藏元素（class 恰好是 upload-input），点击拖拽区才触发。
        require_visible=False,
    ),
    Contract(
        name="upload_preview",
        page="publish",
        candidates=(
            "input.upload-input + div[class*='preview-new']",
            "div[class*='preview-new']",
            "div[class*='preview']",
        ),
        note="上传进度/结果文本所在区域，用于判断'上传成功'",
    ),
    Contract(
        name="title_input",
        page="publish",
        candidates=(
            "input[placeholder*='填写标题']",
            "div.d-input input",
            "input[placeholder*='标题']",
        ),
        note="标题硬上限 20 字",
    ),
    Contract(
        name="title_length_hint",
        page="publish",
        candidates=(
            "div.title-container div.max_suffix",
            "div[class*='title-container'] div[class*='max_suffix']",
        ),
        required=False,
        note="平台自带的 x/20 计数，比我们自己数字数更可信",
    ),
    Contract(
        name="desc_input",
        page="publish",
        candidates=(
            "p[data-placeholder*='输入正文描述']",
            "div[role='textbox'][contenteditable='true']",
            "div.tiptap[contenteditable='true']",
            "div.ql-editor",
        ),
        note="富文本编辑器；先点一下再输入，否则会丢字",
    ),
    Contract(
        name="desc_length_error",
        page="publish",
        candidates=(
            "div.edit-container div.length-error",
            "div[class*='edit-container'] div[class*='length-error']",
        ),
        required=False,
        note="正文超 1000 字时出现",
    ),
    Contract(
        name="topic_suggest",
        page="publish",
        candidates=("div[class*='topic-container']", "#creator-editor-topic-container"),
        required=False,
        note="输入 # 后的话题联想下拉框；等不到就跳过该话题（上游同款降级策略）",
    ),
    Contract(
        name="topic_first_option",
        page="publish",
        candidates=(
            "#creator-editor-topic-container .item",
            "#creator-editor-topic-container div[class*='item']",
        ),
        required=False,
        note="联想下拉的第一项，点它才会把 #话题 变成真话题",
    ),
    Contract(
        name="schedule_switch",
        page="publish",
        candidates=(
            ".post-time-wrapper .d-switch",
            "div[class*='post-time'] div[class*='switch']",
        ),
        note="定时发布开关",
    ),
    Contract(
        name="schedule_input",
        page="publish",
        candidates=(
            ".date-picker-container input",
            "div[class*='date-picker'] input",
        ),
        note="时间输入框；只有打开上面的定时开关后才会渲染，故不算必填",
        required=False,
    ),
    Contract(
        name="visibility_select",
        page="publish",
        candidates=(
            "div.permission-card-wrapper div.d-select-content",
            "div[class*='permission'] div[class*='select-content']",
        ),
        required=False,
        note="可见范围下拉；默认公开可见，不改就等于公开",
    ),
    Contract(
        name="visibility_option",
        page="publish",
        candidates=(
            "div.d-options-wrapper div.d-grid-item div.custom-option",
            "div[class*='options-wrapper'] div[class*='custom-option']",
        ),
        required=False,
        note="下拉里的选项，按文本匹配 公开可见/仅自己可见/仅互关好友可见",
    ),
    Contract(
        name="original_switch_card",
        page="publish",
        # 这个控件没有稳定 class 可锚定，靠文案定位，见 page_ops.set_original()
        candidates=("div.custom-switch-card", "div[class*='switch-card']"),
        required=False,
        note="需要先按文本筛出含「原创声明」的那张卡片，再找卡片内的 d-switch",
    ),
    Contract(
        name="publish_button",
        page="publish",
        candidates=(
            "button:has-text('发布')",
            "xhs-publish-btn",
            ".publish-page-publish-btn button.bg-red",
        ),
        note="新版是 <xhs-publish-btn> 自定义元素，旧版是 button.bg-red，两套都要兼容",
    ),
    Contract(
        name="schedule_button",
        page="publish",
        candidates=(
            "button:has-text('定时发布')",
            "xhs-publish-btn:has-text('定时发布')",
        ),
        note="开启定时后提交按钮文案会变成「定时发布」；未开定时时不存在，故不算必填",
        required=False,
    ),
)

# ------------------------------- 合集 -------------------------------------- #

# 本节契约已于 2026-09 用真实账号**深自检实测**确认（此前是纯猜测）。
# 实测要点：
#   1. 合集控件文案是「选择合集」，且**必须先把视频传上去**才会渲染出来；
#   2. 点开后是一个浮层 .collection-plugin-popover，每个合集是一个 .item（内含 .item-label）；
#   3. 浮层底部还有一个「创建合集」入口，**不能**把它当成合集选项。
#
# 设计立场（与设计方案 4.4 节一致）：**不确定就不选**。
# 进错合集对账号的伤害大于不进合集，所以任何一步不确定都退回"保持未选"。
COLLECTION = (
    Contract(
        name="collection_trigger",
        page="publish",
        candidates=(
            "text=选择合集",       # ← 实测命中，真实文案
            "text=添加合集",
            "text=加入合集",
            "text=添加到合集",
        ),
        required=False,
        note="实测文案为「选择合集」；账号未开通合集功能时该控件不存在，属正常",
    ),
    Contract(
        name="collection_option",
        page="publish",
        # 实测 DOM：
        #   <div class="d-popover collection-plugin-popover">
        #     <div class="collection-plugin-popover-content">
        #       <div class="item"><div class="item-content">
        #            <div class="item-label">风景</div></div></div>   ← 一个合集一项
        #       <div class="popover-footer">… 创建合集 …</div>        ← 新建入口，不是选项
        # 用 .item 而非 .popover-footer，避免把「创建合集」误当成合集。
        candidates=(
            ".collection-plugin-popover .item",
            ".collection-plugin-popover-content .item",
            ".collection-plugin-popover .item-label",
            "div.d-options-wrapper div.d-grid-item div.custom-option",
        ),
        required=False,
        note="实测：弹层为 .collection-plugin-popover，选项是其中 .item；账号没建过合集时列表为空",
    ),
)


ALL_CONTRACTS: Sequence[Contract] = LOGIN + PUBLISH + COLLECTION


# --------------------------------------------------------------------------- #
# 文案常量（页面上的中文提示词，改版时同样可能变）
# --------------------------------------------------------------------------- #


class Text:
    """页面上的文案锚点。用文案定位比用 class 稳，但文案本身也会变，集中放这里。"""

    # 发布 Tab
    TAB_VIDEO = "上传视频"
    TAB_IMAGE = "上传图文"

    # 上传完成判定：预览区域出现这些词之一即视为完成（沿用上游的判定集合）
    UPLOAD_DONE_KEYWORDS = (
        "上传成功",
        "分辨率",
        "重新上传",
        "编辑封面",
        "已上传",
        "已选择",
        "100%",
    )

    # 提交
    BTN_PUBLISH = "发布"
    BTN_SCHEDULE = "定时发布"

    # 可见范围
    VISIBILITY_PUBLIC = "公开可见"
    VISIBILITY_PRIVATE = "仅自己可见"
    VISIBILITY_FRIENDS = "仅互关好友可见"

    # 原创声明
    ORIGINAL_CARD = "原创声明"
    ORIGINAL_CONFIRM = "声明原创"
    ORIGINAL_NOTICE = "原创声明须知"

    # 合集
    COLLECTION_TITLE_HINTS = ("添加合集", "选择合集", "加入合集", "添加到合集")


# 硬约束（来自平台，见设计方案 2.3 节）
MAX_TITLE_LEN = 20
MAX_DESC_LEN = 1000
MAX_TAGS = 10
SCHEDULE_MIN_HOURS = 1
SCHEDULE_MAX_DAYS = 14
