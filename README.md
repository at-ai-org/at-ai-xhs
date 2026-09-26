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

## 当前进度

| 阶段 | 状态 |
| --- | --- |
| 设计方案 | ✅ [`docs/设计方案.md`](docs/设计方案.md) |
| 开源项目调研 | ✅ [`docs/开源项目调研.md`](docs/开源项目调研.md) |
| 架构图 | ✅ [`docs/architecture.html`](docs/architecture.html) |
| 小红书页面操作层 | ✅ [`docs/小红书页面操作层.md`](docs/小红书页面操作层.md) |
| **前后端 Web 服务** | ✅ 扫码登录 / 自检 / 合集 / 发布，全部网页操作 |
| **发布中心实时进度 + 发布记录** | ✅ 每个平台/账号/任务独立进度，发过的都留档 |
| **视频理解 → 文案生成** | ✅ 抽首尾帧 → 视觉模型 → 标题/正文/话题，结果落库到素材 |
| **素材管理 + AI 文案留存** | ✅ 素材库统一管理，文案生成一次后续发布直接带出 |
| **模型设置（OpenAI 兼容）** | ✅ 服务地址/Key/模型/字数上限/发布默认值，网页配置 |
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

启动后打开 **http://localhost:5173** → 左侧菜单 **「小红书工作台」**。

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

### 6. 配置 AI 模型（要用 AI 文案才需要）

打开 **模型设置**（`/#/model-settings`），填三个要素即可：

| 字段 | 说明 |
| --- | --- |
| 服务地址 | 填到 `/v1` 这一层，程序自动拼 `/chat/completions` |
| API Key | 明文存在本机 `model_settings.json`（权限 600），接口只回传掩码 |
| 模型名 | **必须是多模态（能读图）模型**，如 `gpt-4o-mini` / `qwen-vl-max` / `doubao-1.5-vision-pro` |

只要求服务实现了 OpenAI 的 `/chat/completions` 接口——官方 OpenAI、各类中转、
通义/豆包等兼容层、vLLM、Ollama 都能直接用。点「测试连接」会发一个最小请求验证连通性。

## 页面与功能

左侧菜单共 8 项：

| 页面 | 路由 | 作用 |
| --- | --- | --- |
| 首页 | `/#/` | 概览 |
| 账号管理 | `/#/account-management` | 快手 / 抖音 / 视频号 / 小红书的扫码登录、Cookie 上下载、增删改 |
| 素材管理 | `/#/material-management` | 素材库：上传、预览、AI 文案、删除 |
| 发布中心 | `/#/publish-center` | 素材 × 平台 × 账号批量发布 + 实时进度 |
| 发布记录 | `/#/publish-records` | 每次发布的留档，可筛选、看详情与日志 |
| 小红书工作台 | `/#/xhs-workbench` | 账号/自检/合集/发布，小红书专用全流程 |
| 模型设置 | `/#/model-settings` | 视觉模型配置 + 发布默认值 |
| 关于 | `/#/about` | 项目信息 |

### 素材管理（`#/material-management`）

所有待发布视频的**统一入口**：上传（支持多选、逐文件进度）、预览（视频/图片内联播放，
其他格式提供下载）、删除、搜索、分页。

关键列是 **「AI 文案」**——显示 `已有` / `无`。对任意素材点「生成文案」，
后端抽首尾帧交给模型，生成完**把文案存到素材上**，并把素材文件改名成标题
（文件名只保留中文和数字）。之后在发布中心选中这条素材，文案会被**自动带出**，
不用重新生成。

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

**可配置项**（`model_settings.json`，也可在「模型设置」页改）：

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

各平台的合规标注默认值（「模型设置 → 发布默认值」可改）：

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
GET  /xhs/settings                读取模型设置（api_key 只回传掩码）
POST /xhs/settings                保存模型设置（api_key 传空 = 不修改）
POST /xhs/settings/test           连通性测试（会先保存 body 里的 vision 配置）
GET  /xhs/defaults                发布页默认值（默认合集）
```

### 发布中心（`sau_backend_publish.py`，前缀 `/publish`）

```
POST /publish/submit               创建发布批次 → { job_id, tasks[] }
GET  /publish/job/<job_id>         批次实时进度（平台 / 账号 / 任务三层）
GET  /publish/task/<task_id>       单个任务详情（逐步结果 + 完整日志）
GET  /publish/jobs?active=1        最近的批次（刷新页面后据此恢复进度视图）
POST /publish/job/<job_id>/cancel  取消批次里还没开始的任务
GET  /publish/records              发布记录（分页 + 筛选 + 状态统计）
GET  /publish/records/<id>         单条记录详情
GET  /publish/prompts              当前等人输入的请求（短信验证码…）
POST /publish/prompts/<id>         回答一次等人输入（{"value": "123456"} / {"skip": true}）
```

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

13 个测试文件、72 个用例，覆盖小红书页面操作、抖音声明、视频号标注、发布进度/弹窗桥、
浏览器 CLI 派发等。

> ⚠️ 当前 **66 过 / 6 未过**：失败项集中在 `test_sau_browser_cli`（CLI 参数名与本地改造后的
> 上游版本不一致）、`test_douyin_declaration`、`test_xiaohongshu_uploader`（等待超时
> 2000ms → 4000ms 的本地调整未同步到测试）。属于历史遗留的过期断言，与文档/忽略规则无关，
> 待单独修。

## Docker

```bash
docker build -t at-ai-xhs .
docker run -p 5409:5409 at-ai-xhs
```

镜像分两阶段：Node 22 构建前端 → Python 3.10 + Playwright Chromium 跑后端，
前端产物直接由 Flask 托管，只需暴露 `5409`。
Dockerfile 内会用 `conf.example.py` 生成 `conf.py` 并自行 `mkdir videoFile cookiesFile`。

## 目录结构

```
at-ai-xhs/
├── docs/                       设计文档、调研报告、架构图
├── sau_backend.py              Flask 后端（端口 5409，上游接口 + 静态托管）
├── sau_backend_xhs.py          ← 小红书 Web 接口（含 AI 文案、素材、模型设置）
├── sau_backend_publish.py      ← 发布中心：批量子任务 + 实时进度 + 发布记录
├── sau_cli.py                  命令行入口
├── sau_frontend/               Vue3 + Vite 前端（端口 5173）
│   └── src/views/
│       ├── XhsWorkbench.vue         ← 「小红书工作台」
│       ├── MaterialManagement.vue   ← 「素材管理」（含 AI 文案生成）
│       ├── ModelSettings.vue        ← 「模型设置」（视觉模型 + 发布默认值）
│       ├── PublishCenter.vue        ← 「发布中心」（含实时进度）
│       ├── PublishRecords.vue       ← 「发布记录」
│       └── AccountManagement.vue    ← 「账号管理」
├── myUtils/
│   ├── app_settings.py         ← 模型设置读写（含 api_key 掩码）
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
├── tools/                      开发辅助脚本（Vue 绑定检查、素材页校验）
├── tests/                      单元测试
├── db/                         建库脚本与 SQLite 数据库
└── uploader/ utils/ skills/ static/ media/ ...   上游其余模块
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

- 发布任务**没有超时上限**：上游上传器里有若干无上限的等待（等页面跳转、等上传完成），卡住会一直占着执行线程，后面的任务只能排队。真卡住了就重启后端——重启会把残留任务标成「已中断」，记录不会说谎。
- 读取小红书合集**没有独立接口**，要付出一次上传+转码的代价（分钟级），所以结果有缓存；「读缓存」秒回，「刷新合集」才重新抓。
- 自动化发布存在**账号风控风险**，请先用测试号验证，控制频率，并对最终内容人工复核。
- 默认立场是**辅助创作与发布**，不是无人值守发号机。
- 本仓库包含 MIT 许可的第三方代码，原始声明见 `LICENSE.social-auto-upload`。

## 🔒 安全提醒

以下文件**已被 `.gitignore` 排除，请勿提交**：

| 文件 / 目录 | 内容 |
| --- | --- |
| `model_settings.json` | **明文 API Key**（权限 600） |
| `conf.py` | 本机配置 |
| `cookies/`、`cookiesFile/` | **真实登录 Cookie / 登录态**，等于账号凭据 |
| `accounts.ini` | 账号凭据 |
| `db/*.db` | 数据库（含账号与发布记录） |

对外接口一律只回传 `api_key` 的掩码（保留前 4 位与后 4 位，中间固定 6 个 `*`，
形如 `abcd******wxyz`）和一个 `has_key` 布尔值，**不返回明文**。

## 来源与许可

- 上游仓库：<https://github.com/dreammis/social-auto-upload>（MIT）
- 拉取时的 commit、本地改造清单、与上游同步的方法：见 [`PROVENANCE.md`](PROVENANCE.md)
- 上游原始 README：见 [`README.upstream.md`](README.upstream.md)
- 第三方许可声明：见 [`LICENSE.social-auto-upload`](LICENSE.social-auto-upload)
