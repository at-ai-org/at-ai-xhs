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
