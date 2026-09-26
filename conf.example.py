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
