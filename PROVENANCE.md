# 来源说明 / Provenance

本仓库以 **social-auto-upload** 为基底改造而来（fork）。

| 项 | 值 |
| --- | --- |
| 上游仓库 | https://github.com/dreammis/social-auto-upload |
| 拉取时的 commit | `0012d2c355f88f683cc38dde2a2db209e14091bc`（2026-09-03） |
| 上游许可证 | MIT —— 原始声明见 `LICENSE.social-auto-upload`，上游 README 见 `README.upstream.md` |
| 目录结构 | 上游内容已**整体上提到仓库根目录**（不再是 social-auto-upload/ 子目录） |

## 本地改造内容

### 1. 小红书页面操作层（`uploader/xiaohongshu_uploader/`）

- `selectors.py` —— 页面契约注册表（21 条，多候选回退，改版时只改这里）
- `page_ops.py` —— 命名操作层（登录态、上传、标题/正文/话题、**合集**、定时、可见范围、原创、提交、**发布后校验**、自检）
- `main.py` —— 发布编排改用上面的操作层；新增 `xiaohongshu_selftest()` / `xiaohongshu_list_collections()`

### 2. Web 化（全部通过网页操作）

- `sau_backend_xhs.py` —— Flask 蓝图，8 个接口：账号列表 / 扫码登录(SSE) / 校验 / 自检 / 合集 / 发布 / 任务进度 / 任务列表
- `sau_frontend/src/views/XhsWorkbench.vue` —— 「小红书工作台」页面：扫码登录、契约自检、合集读取、发布（含合集/定时/可见范围/原创/dry-run）与逐步进度
- `sau_frontend/src/api/xhs.js` —— 前端 API 模块
- 路由与侧边栏菜单已注册（`/xhs-workbench`）

### 3. 其它改动

- 上传体积上限 160MB → 2GB（`sau_backend.py`，视频素材需要）
- 浏览器安装目录改到项目内 `.browsers/`（`conf.py` / `conf.example.py`），避免依赖用户主目录缓存
- 新增 `examples/xhs_page_ops_smoke.py`（无需账号的冒烟测试）

## 与上游同步

本目录已移除嵌套 `.git`。如需对比或跟进上游修复：

```bash
# 在仓库根目录执行
git remote add upstream https://github.com/dreammis/social-auto-upload.git
git fetch upstream
git diff upstream/main -- uploader/xiaohongshu_uploader/
```

上游对小红书页面 DOM 的修复通常集中在 `uploader/xiaohongshu_uploader/main.py`，值得定期跟进。
