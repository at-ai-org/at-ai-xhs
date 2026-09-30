# at-ai-xhs 镜像：两阶段
#   阶段 1（builder）Node 22 构建前端 → 阶段 2 Python 3.10 + patchright Chromium 跑后端。
# 前端产物由 Flask 同源托管（见 sau_backend.py 的 / 与 /assets/<filename>），所以只需暴露 5409。
#
# 2026-09-30 复核时补掉/修掉的问题（每条都在下面标了原因）：
#   1. 依赖清单缺 4 个 fork 必需包 → 镜像里 `python sau_backend.py` 起不来（已补进 requirements.txt）；
#   2. 前端 API 地址的 sed 已经和现在的代码对不上 → 改成统一"同源绝对路径"；
#   3. 浏览器装的是 playwright 的 headless-shell，而代码用的是 **patchright** 且大量走
#      `channel="chromium"`（= 完整 Chromium）→ 两个驱动各装齐全的浏览器；
#   4. 容器里没建表（db/*.db 被 .dockerignore 排除）→ 启动时幂等补一遍；
#   5. 时区默认 UTC，而发布定时/记录全用本地时间 → 差 8 小时；
#   6. 缺 opencv 需要的 libGL、缺中文字体。
#
# 2026-09-30 追加「背景音乐」功能时复核：ffmpeg 用 pip 装的 imageio-ffmpeg 自带的静态二进制
# （换音轨、探测时长都够用，apt 里那个 ffmpeg 不需要），但 `bgmFile/` 这个运行目录要在
# 镜像和入口脚本里都建出来——它常被单独挂卷，卷会把镜像里的目录遮掉。

# ── 阶段 1：构建前端 ────────────────────────────────────────────────────────
FROM node:22.21.1 AS builder

WORKDIR /app

RUN npm config set registry https://registry.npmmirror.com

COPY sau_frontend .

# 仓库里没有提交 package-lock.json（.gitignore 忽略了它），所以只能用 npm install。
# 想要可复现构建就先 `npm install` 生成 lockfile 并放开 .gitignore/.dockerignore 里的忽略。
RUN npm install --no-audit --no-fund

ENV NODE_ENV=production
ENV PATH=/app/node_modules/.bin:$PATH

# 前端 API 地址：容器里界面和后端同源，所有请求都该走"同源绝对路径"。
#
# 原来那三行 sed 是给旧前端写的，现在会改坏：
#   * `sed 's#localhost:5409##g' .env.production` 把 `VITE_API_BASE_URL=http://localhost:5409`
#     变成 `VITE_API_BASE_URL=http://`（残缺值，但它是 truthy，会覆盖所有兜底）；
#   * 于是 `sau_frontend/src/api/xhs.js:110` 的 `${API_BASE}/xhs/login/stream?...`
#     拼成 `http:///xhs/login/stream?...`——按 URL 规范（浏览器/Node 一致）空主机会被
#     后面那段补上，实际请求打到 **http://xhs/login/stream**，即小红书工作台的扫码登录
#     在容器里必挂；`AccountManagement.vue`、`material.js` 是靠单独 sed 才侥幸对的。
# 现在只做两件事，覆盖全部文件（含以后新增的）：
#   1. 把环境变量置空；2. 把所有 `|| 'http://localhost:5409'` 兜底换成 `|| ''`。
# 结果：`${base}/xhs/...`、axios 的 baseURL 全部解析成 `/xhs/...`，和界面同源。
RUN sed -i "s#^VITE_API_BASE_URL=.*#VITE_API_BASE_URL=#" /app/.env.production \
 && grep -rl "'http://localhost:5409'" /app/src | xargs -r sed -i "s#'http://localhost:5409'#''#g" \
 && ! grep -rqE "(localhost|127\.0\.0\.1):5409" /app/src /app/.env.production

RUN npm run build


# ── 阶段 2：运行时 ──────────────────────────────────────────────────────────
# 钉住 Debian 代号：不写的话基础镜像会随上游漂移，Debian 13 (trixie) 把 libasound2
# 改名成 libasound2t64，这里手写的 apt 列表就会直接构建失败。
FROM python:3.10.19-bookworm

WORKDIR /app

# 时区：整条链路都用**本地时间**——发布记录的 created_at、账号/任务的启动时间，
# 以及最要紧的「定时发布」时间字符串都是 `datetime.now()` 出来的。容器默认 UTC，
# 定时发布就会整体差 8 小时（小红书那条还有 0~20 分钟随机抖动，更看不出来是时区问题）。
ENV TZ=Asia/Shanghai
ENV PYTHONUNBUFFERED=1
# patchright 和 playwright 共用一个环境变量；conf.py 里是 setdefault，所以这里设置的值优先，
# 不会去用项目内的 .browsers/（那个目录在镜像里根本没有）。
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

# 只装浏览器之外还缺的东西：
#   * tzdata          → 上一条 ENV TZ 要生效
#   * libgl1/libglib  → opencv（utils/login_qrcode.py 顶层 `import cv2`，二维码解码要用）
#                       缺 libGL.so.1 会直接 ImportError，四个平台的扫码登录全挂
#   * fonts-noto-cjk  → 页面是中文的；没字体时失败截图全是方块，排障等于瞎
# 其余 Chromium 系统依赖交给 `patchright install --with-deps`（见下），它按发行版维护，
# 比手写 apt 列表更不容易漏（pango/cairo/cups/drm 这些原来都没装）。
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata \
        fonts-noto-cjk \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip config set global.index-url https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple

COPY requirements.txt requirements.txt

# requirements.txt 里已经包含 fork 必需的 patchright / opencv-python / imageio-ffmpeg / segno，
# 以及和 patchright 版本对齐的 playwright（2026-09-30 补齐）。
# 上游原版清单缺这几项，只装它的话 `python sau_backend.py` 在 import 阶段就崩：
#     ModuleNotFoundError: No module named 'patchright'（sau_backend → 发布中心 → uploader/*）
#     ModuleNotFoundError: No module named 'cv2'（utils/login_qrcode.py 顶层 import cv2）
#
# 「合成背景音乐」也**不需要额外装系统 ffmpeg**：换音轨和探测时长用的都是
# imageio-ffmpeg 自带的那个静态二进制（myUtils/background_music.py 的 find_ffmpeg()），
# 和 AI 文案抽首尾帧是同一个 ffmpeg；探测走 `ffmpeg -i`，所以也不需要 ffprobe。
RUN pip install --no-cache-dir -r requirements.txt

# 浏览器：代码里**两个驱动都在用**，都要装，且共用同一个 PLAYWRIGHT_BROWSERS_PATH：
#   * patchright —— 四个平台（小红书/视频号/抖音/快手）的发布路径，
#                   其中 8 处写的是 `channel="chromium"`（完整 Chromium / 新 headless），
#                   只装 headless-shell 时这些启动会报找不到可执行文件；
#   * playwright —— myUtils/auth.py、myUtils/login.py 的扫码登录 / cookie 校验路径，
#                   没写 channel 的默认 headless 用的是 chromium-headless-shell。
# 两者版本已在 requirements.txt 里对齐（都 1.63.0），chromium 只会下载一份，第二条基本是空操作。
# --with-deps 顺手把 Chromium 的系统库装齐（apt 列表交给它维护，比手写更不容易漏）。
RUN patchright install --with-deps chromium chromium-headless-shell \
 && playwright install --with-deps chromium chromium-headless-shell

COPY . .

COPY --from=builder /app/dist/index.html /app
COPY --from=builder /app/dist/assets /app/assets
# public/ 下的 logo.svg 同时充当标签页图标（index.html 里的 <link rel="icon">），
# 由 sau_backend.py 的 /logo.svg 与 /favicon.ico 两个路由从这里读。
COPY --from=builder /app/dist/logo.svg /app/assets

RUN cp conf.example.py conf.py

# 建表脚本复制到 db/ 之外：db/ 常被挂成卷，卷会遮住镜像里的文件，
# 而 db/createTable.py 里的路径是 './database.db'（相对当前目录），必须 cd 到 db/ 再跑。
RUN mkdir -p /app/scripts && cp db/createTable.py /app/scripts/createTable.py

# 目录先建好（视频号 profile、日志、素材、背景音乐、账号 cookie 都在这里落地）
# bgmFile/ 是 2026-09-30 加的「背景音乐」音乐库（与 videoFile/ 分开存：发布中心的
# "选择素材"直接列 videoFile/，音乐混进去会被当成素材选走）。
RUN mkdir -p /app/videoFile /app/bgmFile /app/cookiesFile /app/cookies /app/logs

COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 5409

# 探活用 python，省得为了 curl 再往镜像里装包。
# 注意用 /auth/status 而不是 /xhs/accounts：后端挂了全局登录守卫（myUtils/web_auth.py 的
# before_request），除页面外壳/静态资源外都要 token，探 /xhs/accounts 只会拿到 401 → 容器永远 unhealthy。
# /auth/status 在 PUBLIC_ENDPOINTS 里，未登录也回 200 且不泄漏任何信息。
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5409/auth/status', timeout=4).status == 200 else 1)"

# ⚠️ 必须**单进程**运行：任务表/批次表/通道队列、进度桥、验证码弹窗桥、看门狗 Timer、
# 账号锁全在进程内存里（myUtils/publish_progress.py、publish_prompt.py、browser_lock.py）。
# 换成 `gunicorn -w N` 或多副本，"同账号互斥"和"等人填短信验证码"会立刻失效
# （请求落到另一个 worker 就查不到那个任务）。这里用 Werkzeug（Flask 自带，threaded=True）
# 是刻意的选择，不是忘了换生产服务器。
#
# 另外：后端现在带 Web 登录（myUtils/web_auth.py），首次启动会在日志里打印随机生成的
# admin 密码，存在 settings.json 的 auth 段 —— 容器里请用 `docker logs at-ai-xhs` 取。
# 这也意味着**必须把 settings.json 挂出来**，否则容器重建后密码和 API Key 都会重置。
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "sau_backend.py"]
