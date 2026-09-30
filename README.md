# at-ai-xhs

小红书**视频笔记** AI 发布工具：选一个本地视频 → 自动看懂内容 → 生成标题/正文/话题 → 推荐合集 → 按你定的时间发布。

以 [social-auto-upload](https://github.com/dreammis/social-auto-upload) 为基底改造（见 [`PROVENANCE.md`](PROVENANCE.md)），上游内容已整体上提到本仓库根目录。

**核心立场**：辅助创作与发布，不是无人值守发号机。所有发布都有一道**人工确认闸门**——
提交任务后先停在「待确认」，你看过、改过、点「确认发布」才会真的发出去。

## 它是怎么工作的

```
选素材 ──▶ 抽首帧+尾帧 ──▶ 多模态模型 ──▶ 标题/正文/话题 ──▶ 人工核对 ──▶ 浏览器自动发布
  │            (ffmpeg)      (OpenAI 兼容)      (存到素材上)      (待确认)      (无头/有头)
  └─ 也可以完全跳过 AI，自己手写文案
```

AI 只做「看懂视频 + 起草文案」这一段，**发布动作本身仍然是真人确认过的**。

另有一件跟发布无关、但每条成片都要做的事：**背景音乐**。在「背景音乐」页上传自己的
音乐库，然后在「素材管理」里对一条视频点操作栏的「背景音乐」——原音轨被整条换成这首音乐
（音乐比视频短就循环铺满），成品直接叫「原名-音乐名.mp4」，一眼看出用的是哪首。

## 当前进度

| 阶段 | 状态 |
| --- | --- |
| 设计方案 | ✅ [`docs/设计方案.md`](docs/设计方案.md) |
| 开源项目调研 | ✅ [`docs/开源项目调研.md`](docs/开源项目调研.md) |
| 架构图 | ✅ [`docs/architecture.html`](docs/architecture.html) |
| 小红书页面操作层 | ✅ [`docs/小红书页面操作层.md`](docs/小红书页面操作层.md) |
| **前后端 Web 服务** | ✅ 扫码登录 / 自检 / 合集 / 发布，全部网页操作 |
| **发布中心实时进度 + 发布记录** | ✅ 每个平台/账号/任务独立进度，发过的都留档 |
| **任务手动中断** | ✅ 每个任务都能中断（排队中/正在跑的），卡住不用等 30 分钟看门狗 |
| **重复发布二次确认** | ✅ 同一个账号已发过的素材再发一次，提交前列出来让人确认 |
| **视频理解 → 文案生成** | ✅ 抽首尾帧 → 视觉模型 → 标题/正文/话题，结果落库到素材 |
| **素材管理 + AI 文案留存** | ✅ 素材库统一管理，文案生成一次后续发布直接带出 |
| **背景音乐** | ✅ 上传自己的音乐库，一键把素材原音轨**换成**背景音乐（比视频短就循环），文件名留痕 |
| **设置（OpenAI 兼容）** | ✅ 服务地址/Key/模型/字数上限/发布默认值，网页配置 |
| **账号会话定时续期** | ✅ 每天 0 / 7 / 14 / 20 点自动给全部账号续一次登录态（时间点可在「设置」里改，默认开启） |
| **登录鉴权** | ✅ 账号 `admin` + 首次启动随机密码（存 `settings.json`），全部页面与接口都要登录 |
| 合集自动匹配（算法） | ⬜ 未开始（选择动作已实现，AI 也能按内容选） |
| 编排 / 审核 UI | ⬜ 未开始（单条确认已实现，批量审批未做） |

## 环境要求

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.10 ~ 3.13 | `pyproject.toml` 声明 `>=3.10,<3.14`；开发环境用 3.13.2 |
| Node.js | 20 / 22 | 前端构建与 dev server；Docker 镜像用 22.21.1 |
| ffmpeg | 不需要单独装 | 依赖 `imageio-ffmpeg`，自带静态二进制；找不到时才回退到 PATH |

## 快速开始

### 1. 安装依赖

```bash
# 后端
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 前端
cd sau_frontend && npm install && cd ..
```

### 2. 下载浏览器内核

```bash
PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers" .venv/bin/playwright install chromium
```

> ⚠️ **这里必须显式带上 `PLAYWRIGHT_BROWSERS_PATH`**。`conf.py` 只在**程序运行时**
> 把该变量 `setdefault` 到项目内的 `.browsers/`；而 `playwright install` 是独立 CLI，
> **不会执行 `conf.py`**。不指定的话内核会装到 `~/Library/Caches/ms-playwright`，
> 而运行时却去 `.browsers/` 找，结果是"浏览器不存在"。

`patchright` 与 `playwright` 在本项目中期望的 chromium 版本完全一致（`1243`），
共用同一份 `.browsers/`，所以**装一次两边都能用**。

> ⚠️ 这个"装一次就够"的前提是**两个驱动的版本号相同**。`requirements.txt` 里已把它们钉在同一版
> （`playwright==1.63.0` + `patchright==1.63.0` → chromium `1243`）；若只升其中一个，
> 另一个会去找自己版本的浏览器目录，表现为"Executable doesn't exist / 浏览器不存在"，
> 这时要么两个一起升，要么按上面那条命令重装一份。
> 另外 `patchright` **并不依赖** `playwright`（两者互不相关），但代码里两条路都在用：
> 四个平台的发布走 patchright，`myUtils/auth.py`、`myUtils/login.py` 的扫码登录 / cookie 校验
> 走 playwright，所以两个包都必须装（2026-09-30 之前 `requirements.txt` 漏了 patchright 和
> opencv，照文档装出来的环境起不来：`ModuleNotFoundError: patchright` / `cv2`）。

> `requirements.txt` 是上游 Windows 项目带过来的 **UTF-16 + CRLF** 文件。pip 靠 BOM 能正常读，
> 不用手动转码；但用编辑器打开看到乱码时别慌，属正常现象。

### 3. 生成配置文件

```bash
cp conf.example.py conf.py
```

`conf.py` 每台机器不一样，**已在 `.gitignore` 中排除**。其中
`PLAYWRIGHT_BROWSERS_PATH` 默认指向项目内的 `.browsers/`，
避免依赖用户主目录缓存（受限环境下不可写）。

### 4. 初始化数据库

首次使用前如果 `db/database.db` 是空的，跑一次
（**必须在 `db/` 目录里跑**，脚本用的是相对路径）：

```bash
cd db && ../.venv/bin/python createTable.py && cd ..
```

### 5. 启动

**一键启动（推荐）**：

```bash
./start.sh            # 杀掉已占用端口，启动前后端
./start.sh stop       # 停止前后端
./start.sh status     # 查看运行状态
./start.sh backend    # 只启后端
./start.sh frontend   # 只启前端
```

脚本会自动：结束占用 `5409` / `5173` 的旧进程（优雅退出超时则 `kill -9`，并连同子进程一起清理）、
等待服务真正就绪后才返回、把日志写到 `logs/`、PID 写到 `.run/`。

启动后打开 **http://localhost:5173**，**先用下面的账号密码登录**，再进「小红书工作台」。

<details>
<summary>手动启动（两个终端）</summary>

```bash
# 终端 1 · 后端（Flask，端口 5409）
.venv/bin/python sau_backend.py

# 终端 2 · 前端（Vite，端口 5173）
cd sau_frontend && npm run dev
```

前端用的是 hash 路由，页面地址形如 `http://localhost:5173/#/xhs-workbench`。

</details>

> 🔐 **首次启动会随机生成一个登录密码**并打印在启动终端（后端日志里也有一份），
> 账号固定 `admin`。见下面的[登录与鉴权](#登录与鉴权)。

### 6. 配置 AI 模型（要用 AI 文案才需要）

打开 **设置**（`/#/settings`），填三个要素即可：

| 字段 | 说明 |
| --- | --- |
| 服务地址 | 填到 `/v1` 这一层，程序自动拼 `/chat/completions` |
| API Key | 明文存在本机 `settings.json`（权限 600），接口只回传掩码 |
| 模型名 | **必须是多模态（能读图）模型**，如 `gpt-4o-mini` / `qwen-vl-max` / `doubao-1.5-vision-pro` |

只要求服务实现了 OpenAI 的 `/chat/completions` 接口——官方 OpenAI、各类中转、
通义/豆包等兼容层、vLLM、Ollama 都能直接用。点「测试连接」会发一个最小请求验证连通性。

## 登录与鉴权

整个后台**都要登录才能访问**：页面由前端路由守卫拦，接口由后端 `before_request` 拦，
两边都过不去才算真的进不来（只挡前端等于没挡，直接 curl 就能读数据）。

### 账号与密码

| 项 | 值 |
| --- | --- |
| 账号 | `admin`（固定） |
| 密码 | **首次启动时随机生成的 16 位**，明文存在 `settings.json` 的 `auth.password` |
| 有效期 | 登录态 **7 天**，过期后重新登录 |

密码只在"文件里没有"的时候生成一次，之后每次启动都沿用同一个——**不会**每次重启就换。
首次生成的瞬间会打印在启动终端和 `logs/backend.log`：

```
====================================================================
🔐 已生成后台登录密码（明文保存在 settings.json 的 auth.password）
   账号：admin
   密码：TeSjqjMAtMgB6ZTq
   改密码：编辑 settings.json 的 auth.password，或删掉该字段后重启
====================================================================
```

`auth` 段长这样（`settings.json` 与模型配置**同一个文件**）：

```json
{
  "vision": { "...": "模型配置" },
  "auth": {
    "username": "admin",
    "password": "TeSjqjMAtMgB6ZTq",
    "secret_key": "……token 的签名密钥……",
    "created_at": "2026-09-30 09:51:16"
  }
}
```

| 想干什么 | 怎么做 |
| --- | --- |
| 换成自己的密码 | 改 `auth.password`，重启后端。**改完所有已登录的浏览器都要重新登录吗？不用**——密码只影响下一次登录 |
| 让所有已登录的会话立刻失效 | 改（或删掉）`auth.secret_key`，重启后端。签名密钥一换，已发出的 token 全部作废 |
| 忘掉密码 | 直接看 `settings.json`；或者删掉 `auth.password` 后重启，会重新随机生成一个 |

> 密码和 API Key 一样是**明文**存的（文件权限 600）。这是本地单机工具，
> 但请知悉：**`settings.json` 已被 `.gitignore` 排除，绝不能提交**。

### 挡住的范围

| 范围 | 是否要登录 | 为什么 |
| --- | --- | --- |
| 所有业务接口（`/getFiles`、`/xhs/*`、`/publish/*`、`/getAccounts`…） | ✅ 要 | 这些才是数据 |
| 两个扫码登录 SSE（`/login`、`/xhs/login/stream`） | ✅ 要 | "平台扫码登录"不等于"后台登录"，别被名字绕进去 |
| `GET /`（`index.html`）与 `/assets/*`、`/favicon.ico`、`/logo.svg` | ❌ 不要 | 前端是 SPA，**登录页本身就在这些文件里**。连外壳都要登录的话，浏览器只会拿到一段 401 JSON，登录页永远加载不出来 |
| `POST /auth/login`、`GET /auth/status` | ❌ 不要 | 登录接口本身；`status` 只回答"当前 token 还有效吗"，不返回其他任何信息 |

未登录时接口返回 `401` + `{"code": 401, "msg": "未登录或登录已过期", "data": null}`；
前端拿到 401 会清掉本地登录态并跳回登录页（见 `sau_frontend/src/utils/request.js`）。

### 实现位置

| 文件 | 作用 |
| --- | --- |
| `myUtils/web_auth.py` | 密码生成、账号密码校验、token 签发/校验、全局守卫、`/auth/*` 接口 |
| `sau_backend.py` | 只加了一行 `web_auth.install(app)`（在所有蓝图注册之后） |
| `sau_frontend/src/views/Login.vue` | 登录页 |
| `sau_frontend/src/router/index.js` | 路由守卫（默认要登录，只有 `meta.public` 放行） |
| `sau_frontend/src/utils/session.js` | token 的读写；`withToken()` 给 SSE 用 |

> **SSE 的 token 只能挂在 URL 上**：`EventSource` 无法自定义请求头，
> 所以两个扫码接口用 `?token=xxx`，token 会出现在访问日志里。
> 本地单机可接受；真要多机部署，应改成"先换一次性 ticket 再连"。

## 页面与功能

左侧菜单共 9 项：

| 页面 | 路由 | 作用 |
| --- | --- | --- |
| 首页 | `/#/` | 概览 |
| 账号管理 | `/#/account-management` | 快手 / 抖音 / 视频号 / 小红书的扫码登录、Cookie 上下载、增删改 |
| 素材管理 | `/#/material-management` | 素材库：上传、预览、AI 文案、合成背景音乐、删除 |
| 背景音乐 | `/#/background-music` | 背景音乐库：上传自己的音乐、试听、删除 |
| 发布中心 | `/#/publish-center` | 素材 × 平台 × 账号批量发布 + 实时进度 |
| 发布记录 | `/#/publish-records` | 每次发布的留档，可筛选、看详情与日志 |
| 小红书工作台 | `/#/xhs-workbench` | 账号/自检/合集/发布，小红书专用全流程 |
| 设置 | `/#/settings` | 视觉模型配置、发布默认值、账号会话定时续期（老地址 `/#/model-settings` 仍可用） |
| 关于 | `/#/about` | 项目信息 |

### 账号会话定时续期（「账号管理」的「定时续期」按钮 / 「设置」页）

**为什么需要**：各平台的登录态是**服务端会话**，本地 cookie 文件里那个 `expires`
（常常写到一年后）跟它什么时候被作废毫无关系。视频号实测过：晚上扫码、当晚校验还有效，
第二天早上服务端会话已经没了，而文件里还写着 2027 年（见
[`docs/视频号会话保活.md`](docs/视频号会话保活.md)）。拖住它的唯一办法是**定期真的去用一次**。

**怎么做的**：默认 **每天 00:00 / 07:00 / 14:00 / 20:00**（本地时间），后端依次给
「账号管理」里的每个账号跑一次登录态校验（`check_cookie(..., save_state=True)`）——
判定逻辑和点刷新时是同一套，区别是**会把平台轮换下来的新票据写回 cookie 文件**
（不回写的话新票随浏览器一起销毁，等于白续一次）。范围是账号管理里的**全部账号**，
四个平台一视同仁。

| 事项 | 说明 |
| --- | --- |
| 默认状态 | **开启**。老 `settings.json` 里没有这一段时也按开启处理 |
| 开关在哪 | 「账号管理」页「添加账号」旁边的按钮，以及「设置 → 账号会话续期」——**同一份状态**，两处点哪个都一样 |
| 改时间点 | 「设置 → 账号会话续期」里加/删，格式 `HH:MM`；改完**不用重启后端**（调度器每 30 秒现读一次配置） |
| 正在发布时 | **跳过而不是排队**（续期只是保活，不该把发布堵住），10 分钟后自动补一次，最多补 2 次 |
| 并发 | 账号之间**顺序**执行（一次开一个浏览器）；整机并发上限仍由 `myUtils/browser_lock.py` 统一约束 |
| 不做补跑 | 后端 00:05 才启动的话，00:00 这个点就跳过了。4 个点少一个不影响保活，而"重启就补一发"会平白开一轮浏览器 |
| 记录 | 最近一轮的结果（有效/失效/跳过/出错）显示在「设置」页；**只放在内存**，重启后清零 |

失效的账号会被标成「异常」（和手动刷新的结论一致），需要重新扫码授权。
代码：`myUtils/session_renew.py`，配置读写在 `myUtils/app_settings.py`。

### 素材管理（`#/material-management`）

所有待发布视频的**统一入口**：上传（支持多选、逐文件进度）、预览（视频/图片内联播放，
其他格式提供下载）、删除、搜索、分页。

表格列（从左到右）：**文件名 / 文件大小 / 上传时间 / AI 文案 / 背景音乐 / 操作**。
刻意**没有 UUID 列**——uuid 前缀只存在于磁盘文件名里，对用户没有任何意义；
合成背景音乐后连磁盘名也会换成「标题-音乐名」，界面上不会再出现 uuid。

关键列是 **「AI 文案」**——显示 `已有` / `无`。对任意素材点「生成文案」，
后端抽首尾帧交给模型，生成完**把文案存到素材上**，并把素材文件改名成标题
（文件名只保留中文和数字）。之后在发布中心选中这条素材，文案会被**自动带出**，
不用重新生成。

紧挨着的 **「背景音乐」** 列显示三档状态：`待合成` / `合成中` / `已合成`，
鼠标悬停能看到用的哪首音乐、进度百分比，或合成失败的原因。
操作栏里的「背景音乐」按钮就是合成入口，见下一节。

### 背景音乐（`#/background-music`）与「合成背景音乐」

**先在这个页面上传自己的音乐**（mp3 / wav / m4a / aac / flac / ogg / opus / wma，
可多选、带上传进度，上传时立刻探测时长与音轨，不是音频的文件当场拒掉）。音乐存在
`bgmFile/`，列表里能试听（浏览器直接播放，支持拖动进度条）。

然后在**素材管理**里对某条视频点操作栏的「背景音乐」→ 选一首 → 「开始合成」：

| 行为 | 说明 |
| --- | --- |
| 原音轨 | **整条丢掉**，不是叠加上去（`-map 0:v:0` 只取画面，视频自带的音频不参与输出） |
| 音乐比视频短 | **自动循环播放**铺满整条视频（`-stream_loop -1` + `-t 视频时长`） |
| 音乐比视频长 | 在视频结束处截断，成品时长永远跟着视频 |
| 画质 | 视频流 `-c:v copy` 不重编码；音频重编码成 AAC 192k |
| 进度 | 界面实时显示百分比——那个数字是 ffmpeg 的真实写入位置（`-progress`），不是估算 |
| 结果 | **原地替换**：成品写成 `原名-音乐名.mp4`，原文件删除，AI 文案跟着改名迁移 |
| 反复合成 | 再换一首会先剥掉上一次的「-音乐名」后缀，不会滚成 `标题-音乐A-音乐B.mp4` |
| 与「生成文案」的关系 | 之后再把素材改名成标题时会**保留** `-音乐名`（`标题-音乐名.mp4`），状态行跟着搬家——不然一次生成文案就把"用的哪首音乐"抹掉了 |

合成在后台线程里跑，请求立刻返回任务号，所以几百 MB 的素材也不会卡住页面；
弹窗里随时可以「中断合成」。失败时状态回到「待合成」，原因留在悬停提示里，
目录里不会留下半成品文件。

### 小红书工作台（`#/xhs-workbench`）

五个编号区域，按顺序走一遍就是一条完整发布链路：

| 区域 | 能力 |
| --- | --- |
| ① 账号与登录 | 输入账号标识（如 `me`）→ **扫码登录**（二维码直接显示在网页上）/ 校验登录态 / 账号列表 |
| ② 页面契约自检 | 「浅自检」「深自检（先上传）」检查 21 条页面契约是否还在，页面改版后立刻知道坏在哪条 |
| ③ 合集 | 读取账号可选合集（结果有缓存，「读缓存」秒回） |
| ④ 发布视频笔记 | 上传或从素材库选视频、**AI 生成开关（默认开）**、补充信息、标题、正文、话题、合集、可见范围、定时、原创声明、演练模式 |
| ⑤ 发布任务 | 后台队列，串行执行；每条任务可查看执行步骤与结果 |

**任务状态机**：

```
待执行 ─▶ AI 生成中 ─▶ 待确认 ──(点「确认发布」)──▶ 排队中 ─▶ 发布中 ─▶ 成功 / 失败
                        │
                        ├──(点「重生成」)──▶ 回到 AI 生成中
                        └──(点「放弃」)────▶ 已放弃
```

**发布动作只发生在「确认发布」之后**——`POST /xhs/publish` 本身从不发布，只建任务排队。

「待确认」状态下有三个操作：**确认发布**（真正发出去）、**重生成**（重新跑一次 AI）、**放弃**。

确认弹窗里可选**浏览器模式**：

- **无头**（默认）：不弹窗口、跑得快，但更容易被平台风控提示（「操作过于频繁」等）；
- **有头**：本机弹出真实浏览器窗口，全程可见、更像真人操作，**被风控提示时优先用它重跑**。
  发布期间请不要关闭/最小化那个窗口，也不要手动点页面上的按钮。

### AI 文案生成

**机制**：抽**首帧 + 尾帧**两张图（ffmpeg 抽帧，宽度压到 720px 省 token）→ 交给多模态模型
→ 直接产出 JSON `{title, tags, desc}`。

为什么只看两帧：首帧决定「点不点进来」（封面感、主体、场景），尾帧反映「看完得到什么」；
只发 2 张图 token 成本极低、响应快，对绝大多数笔记已经够用。

生成规则（写死在系统提示词里）：只输出 JSON；标题 ≤ 20 字；话题 3~4 个且不带 `#`；
正文受「正文字数上限」约束（默认 100 字）；emoji 最多 1 个；
**正文里不出现任何暴露素材来源或创作过程的表达**（"首帧""尾帧""画面""这两张图"等）；
看不清的信息（价格、地点、品牌、数字）一律不编造。

**可配置项**（`settings.json`，也可在「设置」页改）：

| 键 | 默认 | 作用 |
| --- | --- | --- |
| `vision.base_url` | `https://api.openai.com/v1` | 服务地址，自动拼 `/chat/completions` |
| `vision.api_key` | `""` | 明文存本机（权限 600）；保存时传空字符串 = **不修改** |
| `vision.model` | `gpt-4o-mini` | 模型名，必须支持读图 |
| `vision.timeout` | `120` | 请求超时（秒） |
| `vision.max_tokens` | `8000` | 最大输出；推理模型的思考过程也占这个额度，给太小会截断 JSON |
| `vision.desc_max_chars` | `100` | 正文字数上限，同时驱动提示词措辞和代码硬截断 |
| `vision.extra_prompt` | `""` | 额外要求，追加到提示词里 |
| `default_collection` | `"风景"` | 发布时默认选中的合集；留空 = 不自动选 |
| `tencent_content_label` | `""` | 视频号「视频标注」；留空 = 不设置这项 |
| `douyin_declaration` | `""` | 抖音「自主声明」；留空 = 不做声明 |

> `vision.enabled` 字段在默认配置里保留，但当前代码**没有任何地方读取它**，属于预留项。

### 发布中心（`#/publish-center`）

把「素材 × 平台 × 账号」一次提交，然后在页面上看实时进度——不是提交完就只剩一句"已提交"：

- 每个 **平台**、每个 **账号**、每条 **素材** 都是独立任务，各自显示状态、当前动作、耗时；
- **按「平台 × 账号」并行执行**：不同平台/账号各跑各的，不用排队等前一个发完；
  同一账号内仍然串行（同账号开两个浏览器会互相踢下线）。整机并发上限见下文「使用须知」；
- 进度来源见 `myUtils/publish_progress.py`：执行器显式上报关键节点，同时旁听上传器的
  日志（"小人正在努力上传视频"…）推断出粗粒度阶段；小红书还能读到页面操作层的逐步结果；
- 展开任务可以看实时日志尾巴；
- **每个任务都能手动中断**（表格每行右侧的「中断」）：排队中的、正在跑的都能中断，
  卡住不用再干等 30 分钟看门狗。中断会**当场**把结论改成「已取消」，并让同一账号后面
  排队的任务立刻往下走。「中断全部未完成」可以一次停掉整批（连正在跑的一起）。
  细节和残留限制见 [`docs/卡住与超时.md`](docs/卡住与超时.md#手动中断用户触发2026-09-新增)；
- **同一个账号已经发过的素材，再发一次要二次确认**：点「开始发布」之前先查一遍发布记录，
  凡是「平台 · 账号 · 素材」此前发过的（失败/取消/中断的不算，还在排队或发布中的算），
  都会列成一张表让你核对——发重了平台上就是两条重复内容，所以要多问一句。
  确认之后才提交，这批任务会在日志里记一句"重复发布…用户已确认重发"，事后翻发布记录
  能分清"知情重发"和"没人提醒就发重了"。判定口径见 `myUtils/publish_records.py` 的
  `find_repeat_publishes`（素材改成标题名前后、小红书工作台发的记录都认得出来）；
- 刷新页面会自动接管还没跑完的批次（`GET /publish/jobs?active=1`）；
- **发布途中要人搭把手时会弹窗**：抖音发布常弹短信验证码，这一步机器过不去。
  后端发布线程会原地等人（`myUtils/publish_prompt.py`），前端轮询到就**直接弹窗**
  让人填 6 位数字，填完发布自动继续；不填/超时就跳过，照旧尝试发布。
  弹窗挂在布局层（`App.vue`），所以不管你停在哪个页面都能弹出来。
  （以前是"自己在项目根目录写个 `verify_code.txt`"，那条路已经删掉了。）

支持的平台：`1 小红书` / `2 视频号` / `3 抖音` / `4 快手`（**不含 B 站**）。

### 发布记录（`#/publish-records`）

记录每一次发布：**账号、平台、时间、状态、标题、素材**，可按来源/平台/账号/状态/关键字/日期筛选，
点「详情」能看到正文、话题、耗时、逐步结果和日志。页面是**只读**的。

**两个入口都会写记录**，用「来源」区分：

| 来源 | 什么时候写 |
| --- | --- |
| 发布中心 | 点「开始发布」提交批次时，每个任务各写一条 |
| 小红书工作台 | 点「确认发布」时写一条（「待确认」「已放弃」不算发布过，不写） |

记录在**提交/确认的那一刻**就落库（`publish_records` 表），所以哪怕后端中途被杀也留得下痕迹：
重新启动时会把残留的「发布中/排队中」标成「已中断」，不会一直挂着骗人。

各平台的合规标注默认值（「设置 → 发布默认值」可改）：

| 平台 | 默认行为 |
| --- | --- |
| 视频号 | **不设置「视频标注」**——实拍视频没有 AI 生成内容，平台也不强制；成片确实含 AI 生成内容时再在设置里选「含AI生成内容」。以前是无脑点「含AI生成内容」，选项不存在时白等 5 秒超时刷一条警告 |
| 抖音 | **不做「自主声明」**——实拍视频没什么可声明的；成片确实含 AI 生成内容（TTS 配音 / AI 字幕 / AI 画面）时再在设置里选「内容由AI生成」。以前上传器无条件兜底声明「内容由AI生成」，等于给实拍视频挂了块 AI 牌子 |

## 接口一览

### 小红书（`sau_backend_xhs.py`，前缀 `/xhs`）

```
GET  /xhs/accounts                账号列表（?check=1 校验 cookie）
GET  /xhs/login/stream            扫码登录（SSE：二维码 + 登录结果）
GET  /xhs/check                   校验单个账号
POST /xhs/selftest                页面契约自检
POST /xhs/collections             读取账号合集
GET  /xhs/materials               素材列表（含已生成的 AI 文案）
POST /xhs/analyze                 抽首尾帧生成文案；save=true 时落库并改名为标题
POST /xhs/publish                 创建发布任务（默认 ai_generate=true，进入「AI 生成中」）
POST /xhs/task/<id>/regenerate    重新生成文案（仅在「待确认」状态可用）
POST /xhs/task/<id>/confirm       确认发布，body 带 headless=false 即「有头浏览器发布」
POST /xhs/task/<id>/cancel        放弃任务
GET  /xhs/task/<id>               任务进度（含每步结果）
GET  /xhs/tasks                   最近任务
GET  /xhs/settings                读取设置（api_key 只回传掩码）
POST /xhs/settings                保存设置（api_key 传空 = 不修改）
POST /xhs/settings/test           连通性测试（会先保存 body 里的 vision 配置）
GET  /xhs/defaults                发布页默认值（默认合集）
```

### 发布中心（`sau_backend_publish.py`，前缀 `/publish`）

```
POST /publish/submit               创建发布批次 → { job_id, tasks[] }
GET  /publish/job/<job_id>         批次实时进度（平台 / 账号 / 任务三层）
GET  /publish/task/<task_id>       单个任务详情（逐步结果 + 完整日志）
GET  /publish/jobs?active=1        最近的批次（刷新页面后据此恢复进度视图）
POST /publish/task/<task_id>/cancel 中断单个任务（排队中 / 正在跑的都能中断）
POST /publish/job/<job_id>/cancel  取消批次里还没开始的任务（带 running=1 连跑着的一起中断）
GET  /publish/records              发布记录（分页 + 筛选 + 状态统计）
GET  /publish/records/<id>         单条记录详情
POST /publish/check-published      这批「素材 × 平台 × 账号」里哪些已经发过了（提交前二次确认用）
GET  /publish/prompts              当前等人输入的请求（短信验证码…）
POST /publish/prompts/<id>         回答一次等人输入（{"value": "123456"} / {"skip": true}）
```

### 背景音乐（`sau_backend_music.py`，前缀 `/music`）

```
GET  /music/list                   音乐库列表
POST /music/upload                 上传音乐（multipart：file / name）
GET  /music/<id>/file              试听 / 播放（支持 Range，可拖动进度条；token 走查询串）
POST /music/<id>/delete            删除音乐（连同 bgmFile/ 里的文件）
POST /music/compose                给一条素材换背景音乐 → { task_id }（body：material / music_id）
GET  /music/task/<task_id>         合成进度（state / progress / message / output）
POST /music/task/<task_id>/cancel  中断合成
GET  /music/statuses               全量「素材 → 背景音乐状态」，素材管理那一列靠它轮询
```

`/getFiles`（素材列表）里每条素材会多带这几个字段，就是「背景音乐」列的来源：
`bgm_status`（`pending` / `running` / `done` / `failed`）、`bgm_status_text`、
`bgm_music`、`bgm_progress`、`bgm_message`（失败原因）。

### 登录（`myUtils/web_auth.py`，前缀 `/auth`）

```
POST /auth/login    登录 → { token, username, expires_in }（无需登录）
GET  /auth/status   当前 token 还有效吗（无需登录，未登录回 authenticated=false）
```

> **下面所有接口都需要登录**，请求带 `Authorization: Bearer <token>`
> （`EventSource` 那类改用 `?token=<token>`）。未登录一律 `401`。

### 通用 / 上游（`sau_backend.py`，端口 5409 根路径）

```
POST /upload            上传素材（返回文件唯一标识）
POST /uploadSave        上传并落库（素材管理用）
GET  /getFiles          素材列表
GET  /getFile           下载/预览单个素材
GET  /deleteFile        删除素材
GET  /getAccounts       账号列表
GET  /getValidAccounts  校验所有 cookie（较慢）
GET  /login             扫码登录（SSE，?type=1|2|3|4）
POST /updateUserinfo    账号改名
GET  /deleteAccount     删除账号
POST /uploadCookie      上传 cookie 文件
GET  /downloadCookie    下载 cookie 文件
POST /postVideo         单条发布
POST /postVideoBatch    批量发布
```

### 账号会话续期（`sau_backend_session.py`，根路径）

```
GET  /session-renew     续期状态：开关 / 时间点 / 上次结果 / 下次时间
POST /session-renew     改开关或时间点（body: enabled / times，只传哪个改哪个）
```

刻意**不放在 `/xhs` 前缀下**：续期的对象是「账号管理」里的全部账号（快手 / 抖音 /
视频号 / 小红书），跟小红书无关。调度本体见 `myUtils/session_renew.py`。

## 命令行（同一套能力）

```bash
.venv/bin/python sau_cli.py xiaohongshu login   --account me --headed
.venv/bin/python sau_cli.py xiaohongshu selftest --account me --debug
.venv/bin/python sau_cli.py xiaohongshu upload-video --account me \
  --file /绝对路径/视频.mp4 --title "标题" --desc "正文" \
  --tags "标签1,标签2" --collection "合集名" --dry-run --debug
```

无需账号的冒烟测试（验证浏览器链路是否正常）：

```bash
.venv/bin/python examples/xhs_page_ops_smoke.py
```

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

28 个测试文件、305 个用例，覆盖小红书页面操作、抖音声明、视频号标注、发布进度/弹窗桥、
浏览器 CLI 派发、背景音乐合成等。另外两块单独说明：
`tests/test_web_auth.py`（登录鉴权：密码只生成一次、过期/伪造 token 拒绝、守卫放行范围、
`auth` 段不被设置接口读写）、`tests/test_background_music.py`（上传 → 合成 → 改名落库，
其中用 FFT 断言成品的主频是**背景音乐**而不是原视频的——用来证明"换掉了音轨"，
而不是"只是把音乐叠上去"，顺带覆盖短音乐循环、长音乐截断、反复合成不叠加后缀）。

浏览器里的验收脚本（需要前后端都已启动）：

```bash
.venv/bin/python tools/verify_login_page.py      # 登录 / 全站鉴权（路由守卫、401 跳转、退出登录）
.venv/bin/python tools/verify_material_page.py   # 素材管理页（会自己先登录）
```

背景音乐/合成链路的验收脚本**自带隔离沙箱**（临时 BASE_DIR + 空库 + 随机端口 +
`dist` 构建产物），不需要先起任何服务，也不会碰你真实的素材、音乐和数据库：

```bash
cd sau_frontend && npm run build                 # 它用 dist 的构建产物，先构建一次
.venv/bin/python tools/verify_bgm_page.py        # 上传音乐 → 合成 → 改名/状态/成品校验
```

> ⚠️ 当前 **300 过 / 5 未过**：失败项集中在 `test_sau_browser_cli`（CLI 参数名与本地改造后的
> 上游版本不一致）、`test_douyin_declaration`。属于历史遗留的过期断言，
> 与文档/忽略规则无关，待单独修。

## Docker

```bash
docker build -t at-ai-xhs .

docker run -d --name at-ai-xhs -p 5409:5409 \
  -e TZ=Asia/Shanghai \
  -v "$PWD/videoFile:/app/videoFile" \
  -v "$PWD/cookiesFile:/app/cookiesFile" \
  -v at-ai-xhs-cookies:/app/cookies \
  -v at-ai-xhs-db:/app/db \
  -v at-ai-xhs-logs:/app/logs \
  -v "$PWD/settings.json:/app/settings.json" \
  at-ai-xhs
```

镜像分两阶段：Node 22 构建前端 → Python 3.10 + patchright Chromium 跑后端，前端产物由 Flask
同源托管，只需暴露 `5409`。Dockerfile 内会用 `conf.example.py` 生成 `conf.py`。

### 卷：哪些东西在容器里，为什么要挂出来

| 路径 | 内容 | 建议 |
|---|---|---|
| `/app/videoFile` | 素材文件（本机已 700MB+） | bind mount（宿主上就有素材） |
| `/app/bgmFile` | 背景音乐文件（用户上传的音乐库；合成后的成品仍在 `videoFile`） | bind mount 或命名卷 |
| `/app/cookiesFile` | 发布中心的账号 cookie（一行一个账号，登记在 `db` 的 `user_info`） | bind mount |
| `/app/cookies` | 小红书工作台/CLI 的 cookie + **视频号的持久化浏览器 profile**（154MB） | **命名卷**，见下面的警告 |
| `/app/db` | `database.db`（素材、账号、发布记录） | 命名卷（宿主上那份 macOS 库不一定兼容） |
| `/app/logs` | 各平台日志 | 命名卷 |
| `/app/settings.json` | 模型设置 + 续期时间点 + 后台登录密码/`secret_key`，**都是明文** | bind mount 或挂卷，否则重建就丢 |

> 挂 `settings.json` 前先确认宿主上**这个文件已经存在**（`ls settings.json`，没有就 `touch` 一个）：
> bind mount 一个不存在的路径时，Docker 会在宿主上建一个**目录**，之后应用读它就报错。

> ⚠️ **不要把宿主的 `cookies/tencent_uploader/uin_*/` 直接挂进 Linux 容器**：那是 Chromium 的
> 完整 profile（cookie 罐 / localStorage / IndexedDB），跨操作系统的内核与路径写法和容器里
> 不一致，等于给视频号换了一台设备——反而是 `docs/视频号会话保活.md` 最怕的事。要么用命名卷
> 让容器自己养，要么在容器里重新扫码登录。
>
> 扫码登录**不需要显示器**：四个平台的二维码都会通过 `/login` 的 SSE 推给网页，
> 在浏览器里扫就行（`utils/login_qrcode.py` 负责解码，容器里也必须有 libGL，
> Dockerfile 已经装上）。
>
> **后台登录**：后端带 Web 登录（`myUtils/web_auth.py`），首次启动会随机生成 admin 密码并打进日志，
> 容器里这样取（也是为什么上面要把 `settings.json` 挂出来——密码、`secret_key`、模型 API Key
> 都在里面）：
>
> ```bash
> docker logs at-ai-xhs 2>&1 | grep -A3 "已生成后台登录密码"
> ```
>
> 顺带一提：除了登录页和静态资源，所有接口都要 token，所以想看容器**存活状态**用
> `curl -s localhost:5409/auth/status`（公开接口），别用 `/xhs/accounts`——那个会回 401。
> Dockerfile 里的 `HEALTHCHECK` 用的就是前者。

### 两个必须知道的约束

1. **只能单进程跑。** 任务表/批次表/通道队列、进度桥、验证码弹窗桥、看门狗 Timer、账号锁
   全在**进程内存**里（`myUtils/publish_progress.py`、`publish_prompt.py`、`browser_lock.py`）。
   换成 `gunicorn -w 4` 或多副本之后，"同一账号不能同时开两个浏览器"和"发布途中弹窗让人填
   抖音短信验证码"会直接失效——请求落到另一个 worker 就查不到那个任务。现在用 Flask 自带的
   Werkzeug（threaded）是刻意的，不是漏改。
2. **时区必须设成 `Asia/Shanghai`。** 发布记录时间、"定时发布"的时间字符串都是
   `datetime.now()` 出来的；容器默认 UTC 会让定时发布整体差 8 小时，而且从小红书的随机抖动里
   根本看不出是时区问题。Dockerfile 已内置 `ENV TZ=Asia/Shanghai`，上面命令里再显式给一次是
   为了覆盖宿主同名变量。

构建时还会做这几件容易漏的事（都在 Dockerfile 注释里写了原因）：

* **依赖**：`requirements.txt` 里补上了 `patchright` / `opencv-python` / `imageio-ffmpeg` / `segno`，
  并把 `playwright` 提到与 patchright 同版。上游那份清单缺这几项，只装它的话容器里
  `python sau_backend.py` 在 import 阶段就崩（`ModuleNotFoundError: patchright` / `cv2`）。
* **浏览器**：`patchright install --with-deps chromium chromium-headless-shell` 和
  `playwright install --with-deps chromium chromium-headless-shell` 两条都跑。代码里两条路都在用：
  发布走 patchright（其中 8 处 `channel="chromium"` 要**完整 Chromium**），扫码登录走 playwright
  （默认 headless 用 headless-shell）。版本已对齐，所以 chromium 只下载一份，第二条基本是空操作。
* **建表**：入口脚本 `docker-entrypoint.sh` 每次启动幂等补建表
  （`db/*.db` 不进镜像，而 `db/` 又常被挂卷，卷会把镜像里的库文件和脚本一起遮住，
  所以建表脚本在构建时另存了一份到 `/app/scripts/`）。

## 目录结构

```
at-ai-xhs/
├── docs/                       设计文档、调研报告、架构图
├── sau_backend.py              Flask 后端（端口 5409，上游接口 + 静态托管）
├── sau_backend_xhs.py          ← 小红书 Web 接口（含 AI 文案、素材、设置）
├── sau_backend_session.py      ← 会话续期接口（/session-renew，管的是全部账号）
├── sau_backend_publish.py      ← 发布中心：批量子任务 + 实时进度 + 发布记录
├── sau_backend_music.py        ← 背景音乐：上传/试听 + 给素材换音轨（异步任务 + 进度）
├── sau_cli.py                  命令行入口
├── sau_frontend/               Vue3 + Vite 前端（端口 5173）
│   └── src/views/
│       ├── XhsWorkbench.vue         ← 「小红书工作台」
│       ├── MaterialManagement.vue   ← 「素材管理」（含 AI 文案生成 + 合成背景音乐）
│       ├── BackgroundMusic.vue      ← 「背景音乐」（上传/试听/删除）
│       ├── ModelSettings.vue        ← 「设置」（视觉模型 + 发布默认值 + 会话续期）
│       ├── PublishCenter.vue        ← 「发布中心」（含实时进度）
│       ├── PublishRecords.vue       ← 「发布记录」
│       └── AccountManagement.vue    ← 「账号管理」
├── myUtils/
│   ├── app_settings.py         ← 设置读写（含 api_key 掩码 / 续期时间点归一化）
│   ├── session_renew.py        ← 账号会话定时续期（默认 0/7/14/20 点）
│   ├── background_music.py     ← 背景音乐存储 + ffmpeg 换音轨（含真实进度解析）
│   ├── browser_lock.py         ← 按账号互斥 + 并发上限（工作台与发布中心共用）
│   ├── publish_progress.py     ← 发布进度上报桥（显式上报 + 日志旁听）
│   ├── publish_prompt.py       ← 发布途中「要人输入」的桥（抖音短信验证码弹窗）
│   └── publish_records.py      ← 发布记录存储层（两个入口共用一张表）
├── uploader/xiaohongshu_uploader/
│   ├── selectors.py            ← 页面契约注册表（21 条）
│   ├── page_ops.py             ← 页面操作层
│   ├── ai_copywriter.py        ← AI 文案生成（抽首尾帧 → 视觉模型）
│   └── main.py                 ← 发布编排
├── examples/xhs_page_ops_smoke.py   ← 无账号冒烟测试
├── tools/                      开发辅助脚本（Vue 绑定检查、素材页/背景音乐页验收）
├── tests/                      单元测试
├── db/                         建库脚本与 SQLite 数据库
├── videoFile/                  素材文件（上传的成品都在这里，已 gitignore）
├── bgmFile/                    背景音乐文件（和素材分开存，见 myUtils/background_music.py）
└── uploader/ utils/ skills/ ...   上游其余模块
    （上游 README 的配图目录 media/、static/、旧说明目录 sau_backend/ 已于 2026-09-30 清理）
```

## 环境变量

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `SAU_MAX_CONCURRENT_BROWSERS` | `4` | 整机同时最多开几个浏览器（工作台与发布中心共用） |
| `BACKEND_PORT` | `5409` | `start.sh` 启动的后端端口 |
| `FRONTEND_PORT` | `5173` | `start.sh` 启动的前端端口 |
| `PLAYWRIGHT_BROWSERS_PATH` | `<项目>/.browsers` | 浏览器内核安装位置，由 `conf.py` 设默认值 |
| `SAU_XHS_CREATOR_BASE_URL` | `https://creator.xiaohongshu.com` | 小红书创作平台地址 |
| `DOUYIN_COOKIE_AUTH_HEADLESS` | `true` | 抖音 cookie 校验是否用无头浏览器 |
| `SAU_BGM_COMPOSE_TIMEOUT` | `1800` | 单次「合成背景音乐」的上限秒数，到点杀掉 ffmpeg 并回落成「待合成」 |
| `YT_PROXY` | `None` | YouTube 上传用的代理（`conf.py` 内配置） |

## ⚠️ 使用须知

- **平台限制在页面操作层兜底**：标题 20 字、正文 1000 字超出会被**截断并告警**；
  话题默认最多 10 个；定时时间距现在不足 1 小时会**降级为立即发布**，超过 14 天会报错。
  这些限制定义在 `selectors.py`，由 `page_ops.py` 执行——接口层不做拦截。
- **同一账号只允许一个网页端会话**：工具登录后，别再在别的浏览器登小红书网页版，否则会把工具踢下线（手机 App 不受影响）。
- 发布任务是**按平台/账号并行**跑的：每个「平台 × 账号」一条执行通道，通道之间互不等待；
  同一个账号内部仍然串行（同账号开两个浏览器会互相踢下线）。整机同时开的浏览器数另有上限，
  默认 4，用环境变量 `SAU_MAX_CONCURRENT_BROWSERS` 调（机器吃不住就调小）：

  ```bash
  SAU_MAX_CONCURRENT_BROWSERS=2 ./start.sh
  ```

- 发布任务**有 30 分钟看门狗，也能手动中断**：上游上传器里有若干无上限的等待（等页面跳转、等上传完成），卡住的那个线程 Python 杀不掉，会一直占着执行线程和账号锁。但结论不会骗人——看门狗到点判失败，你也可以随时在发布中心点「中断」，两者都会当即收尾并让同账号后面排队的任务继续跑。**要彻底回收那个账号的资源（不再等锁），仍然只有重启后端**，详见 [`docs/卡住与超时.md`](docs/卡住与超时.md)。
- 读取小红书合集**没有独立接口**，要付出一次上传+转码的代价（分钟级），所以结果有缓存；「读缓存」秒回，「刷新合集」才重新抓。
- 自动化发布存在**账号风控风险**，请先用测试号验证，控制频率，并对最终内容人工复核。
- 默认立场是**辅助创作与发布**，不是无人值守发号机。
- 本仓库包含 MIT 许可的第三方代码，原始声明见 `LICENSE.social-auto-upload`。

## 🔒 安全提醒

以下文件**已被 `.gitignore` 排除，请勿提交**：

| 文件 / 目录 | 内容 |
| --- | --- |
| `settings.json` | **明文 API Key + 明文后台登录密码**（权限 600） |
| `conf.py` | 本机配置 |
| `cookies/`、`cookiesFile/` | **真实登录 Cookie / 登录态**，等于账号凭据 |
| `accounts.ini` | 账号凭据 |
| `db/*.db` | 数据库（含账号与发布记录） |

对外接口一律只回传 `api_key` 的掩码（保留前 4 位与后 4 位，中间固定 6 个 `*`，
形如 `abcd******wxyz`）和一个 `has_key` 布尔值，**不返回明文**。

`settings.json` 里的 `auth` 段（登录密码 + 签名密钥）**整个不会出现在任何接口响应里**，
也不能通过「设置」的保存接口改写——否则等于给了一个"不用登录就能改登录密码"的后门。
这两条都有测试盯着（`tests/test_web_auth.py` 的 `TestSettingsApiDoesNotLeakAuth`）。

## 来源与许可

- 上游仓库：<https://github.com/dreammis/social-auto-upload>（MIT）
- 拉取时的 commit、本地改造清单、与上游同步的方法：见 [`PROVENANCE.md`](PROVENANCE.md)
- 上游原始 README：<https://github.com/dreammis/social-auto-upload#readme>（本地副本已于 2026-09-30 删除，见「目录结构」的说明）
- 第三方许可声明：见 [`LICENSE.social-auto-upload`](LICENSE.social-auto-upload)
