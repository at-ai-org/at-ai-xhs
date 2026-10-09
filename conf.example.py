from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
XHS_SERVER = "http://127.0.0.1:11901"  # only used by xhs-related flows
LOCAL_CHROME_PATH = ""  # optional, e.g. C:/Program Files/Google/Chrome/Application/chrome.exe
LOCAL_CHROME_HEADLESS = True  # default headless behavior for uploader/examples
DEBUG_MODE = True  # default debug behavior
# Optional proxy for the YouTube uploader. Where youtube.com is blocked, direct
# connections time out and the (patchright) chromium does NOT use the system proxy.
# Point this at your local proxy port, e.g. "http://127.0.0.1:7890". None = no proxy.
YT_PROXY = None

# ---- at-ai-xhs 改造新增：浏览器安装位置 ----
# 默认缓存目录在用户主目录（~/Library/Caches/ms-playwright），在受限环境下不可写。
# 这里统一放到项目内的 .browsers/，CLI 和 Web 后端都能用同一份浏览器。
import os as _os
_os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(__file__).parent.resolve() / ".browsers"))

# ---- at-ai-xhs 改造新增：视频号（channels.weixin.qq.com）会话策略 ----
# 视频号扫码登录拿到的是一条**服务端会话**，本地 cookie 上的 expires（登录时刻 +400 天）
# 跟它什么时候被作废毫无关系。所以只能靠两件事拖住它：
#   1. 每条用过的路径都把 cookie 回写（服务端轮换的票据绝不丢）；
#   2. 登录 / 校验 / 发布共用同一个持久化浏览器 profile（同一个"设备"）。
# 实现见 uploader/tencent_uploader/session.py。
#
# TENCENT_HEADLESS：视频号「登录 / 校验 / 发布」三处共用的无头开关。
#   默认 True = 保持既有行为。上游抖音踩过"无头撞反爬 → 校验误报 cookie 失效"的坑
#   （upstream commit 914a5f0），patchright 官方的最佳实践也建议有头（headless=False）。
#   视频号要不要照做尚无定论，所以留成开关做 A/B：设成 False 即三处全部有头跑一阵，
#   对比掉线频率。不改文件也行，用环境变量临时覆盖：SAU_TENCENT_HEADLESS=0
#
# TENCENT_PROFILE_ROOT：持久化浏览器 profile 的根目录，每个微信号一个子目录
#   （uin_<wxuin>），里面是 cookie 罐 / localStorage / IndexedDB。
#   删掉某个子目录 = 让那个账号"换一台设备"，下次任务会重新建一个空 profile。
def _env_flag(name: str, default: bool) -> bool:
    raw = _os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


TENCENT_HEADLESS = _env_flag("SAU_TENCENT_HEADLESS", True)
TENCENT_PROFILE_ROOT = BASE_DIR / "cookies" / "tencent_uploader"

# ---- at-ai-xhs 改造新增：小红书发布行为收敛 ----
# 背景：2026-03-10 小红书「薯管家」发布《关于打击AI托管运营账号的治理公告》，
# 把"利用技术手段模拟真人"本身列为违规，并对"与真人操作习惯不符"的行为特征
# 分档处罚（偶发→警告限流 / 全量AI代发→封禁）。下面几项用于收敛发布链路上
# 最扎眼的机器特征。注意：这些是**降低异常特征**，不是反检测——账号安全依赖的是
# 自动化占比真的下降（人工确认闸门 + 真人编辑），不是让机器装得更像人。

# XHS_PUBLISH_MAX_ATTEMPTS：发布按钮点击次数上限。
#   上游是 `while True` + 0.5s 无限重试：被平台校验（字数/违规/风控）拦下时会变成
#   "每 0.5 秒点一次、永不停止"的脚本特征，而且不会报错退出。改成有界重试。
#   （注：发布一律有头是**硬编码**的，刻意不设开关——见 settings.py 的说明。）
XHS_PUBLISH_MAX_ATTEMPTS = 3

# XHS_HUMAN_TYPE_DELAY：标题/正文逐字输入的每字延时区间（毫秒）。
#   None = 关闭人性化输入，退回原来的瞬时输入。
#   参考：真人中文输入约 5~10 字/秒，即 100~200ms/字；这里取下限偏快，兼顾可接受的总时长。
XHS_HUMAN_TYPE_DELAY = (45, 140)

# XHS_HUMAN_TYPE_BUDGET：单次人性化输入的总时长上限（秒）。
#   长正文按上面的区间逐字打会很久（1000 字 ≈ 90s），超过预算就自动等比压缩每字延时，
#   保证发布不会被"打得太慢"卡住。
XHS_HUMAN_TYPE_BUDGET = 150.0

# XHS_SCHEDULE_JITTER_MINUTES：定时发布的随机抖动上限（分钟）。
#   真人预约发布不会卡在 20:00:00 这种整点整分，机器会。这里把所选时间往后随机
#   推 0~N 分钟以避开整点特征。设 0 = 关闭抖动（实际发布时刻严格等于所选时刻）。
XHS_SCHEDULE_JITTER_MINUTES = 20

# XHS_LOCALE / XHS_TIMEZONE / XHS_VIEWPORT：浏览器上下文里跟"人"有关的几项。
#   默认全空 = 不覆盖，沿用 Playwright 默认值。中国大陆账号建议至少设上前两项——
#   Playwright 默认 locale 常是 en-US，与中文 IP/账号是个明显的不一致信号。
#   时区要与本机一致，否则"浏览器时区 ≠ 系统时区"反而是新破绽。
#   注意：这里**刻意不设 user_agent**。硬编码 UA 一旦与真实内核版本对不上，
#   本身就是更强的指纹；有头模式下直接用真实内核的 UA 最稳。
XHS_LOCALE = "zh-CN"
XHS_TIMEZONE = "Asia/Shanghai"
XHS_VIEWPORT = {"width": 1440, "height": 900}

# ---- at-ai-xhs 改造新增：拼多多视频（多多视频）----
#
# 通道结论（2026-10 调研，详见 docs/拼多多视频接入方案.md）：
#   拼多多开放平台**没有**视频发布能力，能用的只有官方 PC 后台 `live.pinduoduo.com`。
#   它一套后台三种身份（商家 / 达人 / 机构），**登录方式各不相同**：
#     * 达人：/login/anchor，App 扫码（token_type=2）——本项目只做这条；
#     * 商家：账号密码 / 短信；
#     * 机构：mcn.pinduoduo.com（另一套域名，仅企业主体）。
#   发布页里的所有 `/api/backbone/*` 请求都带平台自己算的签名（anti-content），
#   所以**不要**试图绕过页面直连接口，老老实实走 DOM 自动化（和快手那条路一致：
#   签名交给页面自己算，我们只负责填表、点按钮、读结果）。
#: **真正的上传页**。2026-10-08 用真实登录态核对过：
#: `/n-creator/video/short-video` 只是「发布视频」的**活动/任务落地页**（页面上一个输入框都没有，
#: 文案是"第1步 点击底部'发布视频'"）；点它的「立即发布短视频」才会跳到 `home`，
#: 那里才有「上传视频 / 添加视频」和整张视频卡片。走错地址的后果是登录态校验误判失效。
PDD_PUBLISH_URL = "https://live.pinduoduo.com/n-creator/video/home"
#: 落地页地址（`short-video`）。只有当 `home` 打不开上传区时才用它兜底：
#: 进落地页 → 点「立即发布短视频」→ 跳回 `home`（见 `_ensure_upload_page`）。
PDD_PUBLISH_URL_LEGACY = "https://live.pinduoduo.com/n-creator/video/short-video"
PDD_LOGIN_URL = "https://live.pinduoduo.com/login/anchor"
#: 未登录时平台会跳到 /login?isNewCreatorFrom=…&referUrl=…，URL 里出现这个片段即失效。
PDD_LOGIN_URL_MARKER = "/login"
#: 内容管理页（发布后校验作品是否真的出现）。
PDD_MANAGE_URL = "https://live.pinduoduo.com/n-creator/video/video-manage"

# PDD_HEADLESS：拼多多「登录 / 校验 / 发布」共用的无头开关。
#   默认 True = 和其余平台一致；设 False 或在环境变量里给 SAU_PDD_HEADLESS=0 可切成有头，
#   首次接入（选择器还没被真实页面验证过）建议有头跑一次，直接看页面比翻日志快。
PDD_HEADLESS = _env_flag("SAU_PDD_HEADLESS", True)

# PDD_DEBUG_DUMP：每一步都往 pdd_debug/ 里存一份 HTML + 全页截图。
#   为什么默认从宽：拼多多这条链路是本项目第一个**在写代码时拿不到登录态页面**的平台
#   （发布页必须登录才渲染），选择器只能按平台前端产物里扒出来的文案先写一版。
#   所以第一次用真实账号跑时，把开关打开（SAU_PDD_DUMP=1），失败时直接把 dump 目录给我，
#   改选择器一次到位，而不是靠猜。
PDD_DEBUG_DUMP = _env_flag("SAU_PDD_DUMP", False)
PDD_DEBUG_DIR = "pdd_debug"

# 发布页的硬约束（从平台发布页前端产物里读出来的，不是猜的）。
#
# ⚠️ 最容易搞反的一条：**视频发布没有「标题」字段**。
#   平台的发布载荷里 `title` 只出现在 `feedType === IMAGE`（图文）那一支，
#   视频那一支只有 `desc / cover / vid / scheduleTime / goodsIdList / cutStartTime…`；
#   发布页上那个 placeholder="添加标题" 的输入框，渲染条件也是 `feedType === IMAGE`。
#   所以发布中心的「标题」**必须并进描述**，否则用户写的标题无处安放（见
#   uploader/pdd_uploader/main.py 的 _compose_desc）。
#   标题 ≤40 字 / 不能带 # 这两条是**图文**的校验规则，这里保留是为了兜住"平台哪天给
#   视频也加上标题框"（驱动检测到标题框就填，检测不到就并进描述）。
PDD_TITLE_MAX = 40
PDD_DESC_MAX = 500
#: 单个话题最长字数（平台的联想输入会提示「话题长度不可超过15个字」）。
PDD_TOPIC_MAX = 15
#: 一条视频最多能带几个话题（平台提示「最多可以添加5个相关话题」，编辑器里 limit:5）。
PDD_TOPIC_MAX_COUNT = 5

#: 封面要求（平台文案：「需上传封面比例为3:4且分辨率大于720P；文件大小不超过5M」）。
#: 自动生成的封面（抽视频首帧）必须按这个规格裁/补，否则平台不收。
PDD_COVER_RATIO = (3, 4)
PDD_COVER_MIN_WIDTH = 720
PDD_COVER_MAX_MB = 5.0

#: 视频要求（平台文案：mp4 优先、1080P 及以上、帧率 ≥ 24、码率 ≥ 6Mbps；竖版更容易拿到流量）。
#: 这几条只做提示，不拦发布——本地转码不在本工具的职责范围内。
PDD_VIDEO_HINT = "mp4 / 1080P 及以上 / 帧率 ≥ 24 / 码率 ≥ 6Mbps / 竖版更有流量"

# PDD_CONTENT_DECLARATION：**内容声明**选哪一项。
#
# 这是拼多多的**必选项**（发布页上带红星 `*`）：`anchorDeclarationType` 为空时，
# 平台点「发布」**只弹一句警告、连请求都不发**（文案「请选择内容声明」）。
# 首次真实发布两次都失败就是卡在这里——当时驱动没设它，而提示层类名（`TST_notice*`）
# 又没被日志抓到，于是表现为"点了发布没回执"。
# 可选值来自发布页初始化接口 `/api/backbone/video/publish/page/query` 的
# `anchorDeclarationList`（2026-10-08 实测）：
#     内容无需标注(11) / 含AI生成内容(1) / 含虚构演绎内容(5) /
#     内容含营销信息(7) / 内容为转载(3) / 个人观点，仅供参考(8)
# 默认选「内容无需标注」——和抖音「自主声明」、视频号「视频标注」的既有取舍一致：
# 实拍素材不主动挂"含 AI 生成内容"的牌子；**成片确实含 AI 生成画面时**再改成
# 「含AI生成内容」。文案由 AI 代写不算（平台这一项说的是内容本身）。
PDD_CONTENT_DECLARATION = "内容无需标注"

# PDD_UPLOAD_STALL_SECONDS / PDD_UPLOAD_RETRIES：**上传卡住的检测与自救**。
#
# 背景（2026-10-09 实测）：一批 22 条里有 3 条卡在"文件已选中、平台侧再没动静"——
# 素材本身没问题（4MB / 10~25 秒），草稿箱里也没有，就是平台侧那次上传没响应；
# 而当时的等待逻辑是**安静地等满 10 分钟**，界面上只有转圈，用户只能手动中断。
#
# 现在：超过 PDD_UPLOAD_STALL_SECONDS 秒没有任何进展（进度文案没变、视频卡片也没出来）
# 就判定卡住 → 重新加载页面 + 重新选文件上传一次；再卡住就带明确原因快速失败。
# 正常情况这条链路只要 10~15 秒，所以 120 秒的阈值有 10 倍余量。
PDD_UPLOAD_STALL_SECONDS = int(_os.environ.get("SAU_PDD_UPLOAD_STALL") or 120)
PDD_UPLOAD_RETRIES = int(_os.environ.get("SAU_PDD_UPLOAD_RETRIES") or 1)
