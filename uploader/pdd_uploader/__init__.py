from pathlib import Path

from conf import BASE_DIR

# 拼多多的登录态存在 cookiesFile/<uuid>.json（和快手/抖音同一种形态，
# 不是视频号那种持久化 profile），这里留一个目录给"有头模式手动跑"时的临时产物。
Path(BASE_DIR / "cookies" / "pdd_uploader").mkdir(exist_ok=True)
