# -*- coding: utf-8 -*-
"""拼多多发布链路的**离线端到端自测**：用本地假发布页把 `PddVideo` 整条流程跑一遍。

为什么要有这个东西
------------------
拼多多的发布页必须登录才渲染，所以接入时（2026-10）**没有真实页面可用**，
但"没有真实页面"不等于"只能等有了账号再验"：发布链路上真正容易写错的不是选择器，
而是**流程逻辑**——等多久、什么时候算成功、什么时候算被拒、定时会不会被静默降级、
文案到底写进了哪个元素。这些都可以在一张自己写的假页面上验，而且验得比真页面更狠
（真页面不会配合你演出"点了发布却弹出拒稿"）。

**假页面按平台真实结构搭**，不是凭空捏的。下面这些类名/文案都来自它的前端产物：

| 页面元素 | 真实来源 |
| --- | --- |
| 描述编辑器 `text-edit_saboIpt__*`（**contenteditable 富文本**，不是 textarea） | `text-edit` CSS Module + 组件 chunk 5088 |
| 视频卡片 `video-list_itemWrap__*`、卡片上的「发布」按钮 `video-list_singlePublish__*` | `video-list` CSS Module + chunk 2990 |
| 已发布状态 `video-list_mask__*` | 同上（发布成功后平台给卡片盖一层） |
| 发布接口 `POST /api/backbone/video/publish` | chunk 2990 里的 `B={VIDEO:"/api/backbone/video/publish"}` |
| 封面入口 `custom-img-pick_trigger__*`、「确认封面裁剪」 | `custom-img-pick` CSS Module + 封面弹窗文案 |
| 上传区 `no-video_draggerWrap__*`、「添加视频」 | `no-video` / `AddVideo` CSS Module |
| **没有标题输入框** | 平台的标题框只在 `feedType === IMAGE`（图文）渲染；视频发布载荷里根本没有 `title` 字段 |

场景
----
| # | 场景 | 假页面的行为 | 期望结果 |
| --- | --- | --- | --- |
| ① | 正常发布 | 选完文件先显示「上传中」，之后才出现卡片；点发布 → 接口回执 OK、卡片变已发布 | 成功，且服务端收到的文案/封面/文件名与提交的一致 |
| ② | 定时发布 | 同上，另记录「是否切到定时 + 定时时间」 | 成功，且服务端确认收到定时时间 |
| ③ | 接口正常回执 | 有封面时接口回 `{"success":true}` | 成功（④⑦ 的对照组） |
| ④ | 登录失效 | 发布页直接是登录页 | 抛错并提示重新扫码，**不提交任何东西** |
| ⑤ | 上传卡住 | 选完文件后卡片永不出现 | 超时判失败，**不继续往下填、不提交** |
| ⑥ | 没有定时控件 | 页面上没有「定时发布」，而任务是定时的 | 抛错，**绝不降级成立即发布**（服务端 0 次提交） |
| ⑦ | 缺封面 | 没有封面时接口回「请添加封面」，补上封面后放行 | 驱动自动用视频首帧生成合规封面并**重试一次**，最终成功 |
| ⑧ | **发布中心接线** | 走 `sau_backend_publish._build_tasks` → `_invoke_uploader`（生产代码路径），不是直接调驱动 | 任务从「素材 × 平台5 × 账号」正确拼出，并真的发出去 |
| ⑩ | **上传卡住** | 第一次选完文件平台永远不响应（真机实测每 5~8 条出现一次） | 判定卡住 → 重新加载并**自动重传** → 成功；不再安静地等 10 分钟 |
| ⑨ | **缺内容声明** | 页面没有「内容声明」控件，点发布只弹 `TST_notice` 警告、不发请求（**真机行为**） | 抛错并点名「内容声明」，**平台 0 次提交**——不能报"点了发布没回执" |

用法：
    .venv/bin/python tools/verify_pdd_publish_flow_offline.py
    .venv/bin/python tools/verify_pdd_publish_flow_offline.py --headed   # 看着它跑
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

# 脚本在 tools/ 下，仓库根要进 sys.path 才能 import conf（它会设置 PLAYWRIGHT_BROWSERS_PATH）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import conf  # noqa: F401,E402  # 导入它会把 PLAYWRIGHT_BROWSERS_PATH 指到项目内 .browsers/
from uploader.pdd_uploader import main as pdd_main  # noqa: E402

# --------------------------------------------------------------------------- #
# 假发布页（类名与文案照平台前端产物抄）
# --------------------------------------------------------------------------- #

FAKE_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>多多视频｜多多直播</title>
<style>
  body { font-family: sans-serif; padding: 24px; }
  .hidden { display: none; }
  .pdd-modal { position: fixed; inset: 0; background: rgba(0,0,0,.4); }
  .pdd-modal .box { background: #fff; margin: 120px auto; padding: 24px; width: 460px; }
  .pdd-toast { position: fixed; top: 20px; left: 50%; transform: translateX(-50%);
               background: #333; color: #fff; padding: 8px 16px; z-index: 99; }
  [contenteditable] { border: 1px solid #ddd; min-height: 66px; padding: 8px; display: block; }
</style></head>
<body>
  <h1>多多视频｜多多直播</h1>

  <!-- 草稿残留弹窗：驱动必须点它的「取消」，而不是把上一次的半成品发出去 -->
  <div id="draft-modal" class="pdd-modal %DRAFT_CLASS%">
    <div class="box">
      <p>上次有 1 个视频上传成功但未发布，是否继续发布</p>
      <button id="draft-cancel" type="button">取消</button>
      <button id="draft-continue" type="button">仍要发布</button>
    </div>
  </div>

  <!-- 上传区（真实类名：no-video_draggerWrap__* / AddVideo_label__*） -->
  <div id="upload-area" class="no-video_draggerWrap__Uife7">
    <div class="no-video_imgTitle__HjvC2">上传视频</div>
    <div class="no-video_desc__E1aVx">点击上传或直接将 视频 拖拽至此区域</div>
    <input id="file" type="file" accept=".mp4,.wmv,.mov,.avi,.m4v">
    <button id="add-video" type="button" class="AddVideo_label__IFnFk">添加视频</button>
    <div id="progress" class="video-list_uploadPercent__iKC05 hidden">上传中…</div>
  </div>

  <!-- 视频卡片（video-list_itemWrap__*）。注意**没有标题输入框**：平台视频发布没有标题字段 -->
  <div id="card" class="video-list_itemWrap__7xLB4 hidden">
    <div class="video-list_fileName___Diex">自测素材.mp4</div>

    <div class="video-list_coverImage__4_Vre">
      <button id="cover-entry" type="button">编辑封面</button>
    </div>
    <!-- 封面弹窗（真实结构）：默认停在「视频截取」，点「本地上传」才出现图片输入框 -->
    <div id="cover-modal" class="pdd-modal hidden">
      <div class="box">
        <div class="cover-tab" id="tab-cut">视频截取</div>
        <div class="cover-tab" id="tab-upload">本地上传</div>
        <div id="cover-upload-area" class="hidden">
          <input id="cover-file" type="file" accept=".jpg,.jpeg,.png">
          <div>点击上传或直接将文件拖入此区域</div>
        </div>
        <button id="cover-confirm" type="button">确定</button>
        <button id="cover-cancel" type="button">取消</button>
      </div>
    </div>

    <!-- 描述编辑器：平台是富文本 contenteditable（text-edit_saboIpt__*），不是 textarea -->
    <div class="text-edit_textContent__XWaUD">
      <div class="text-edit_saboIpt__VtgAp" style="height:66px">
        <div id="desc-hack-placeholder" class="text-edit_hackPlaceholder__NJLYg">合适的视频描述，将获得更多流量推广</div>
        <div id="desc-editor" contenteditable="true" class="text-edit_emptyReactSabo__mYM__"></div>
      </div>
      <div class="text-edit_saboBottom__wKI8r">
        <div class="text-edit_addTopic__me2OE"># 添加话题</div>
        <div class="text-edit_length__1SwVD"><span id="desc-length">0</span> / 500</div>
      </div>
    </div>

    <!-- 内容声明：平台必选项（真实页面：ContentDeclaration_statement__* 里一个 beast-core-select 下拉，
         选中的值渲染在 input.value 里——innerText 读不到，这是个真踩过的坑） -->
    <div class="%DECLARATION_CLASS%">
      <div class="ContentDeclaration_statementTitle__Op45U"><span class="ContentDeclaration_required__za7O7">*</span>内容声明</div>
      <div data-testid="beast-core-select" class="ST_outerWrapper_5-180-0" tabindex="0">
        <input readonly placeholder="请选择" class="IPT_input_5-180-0" value="">
      </div>
    </div>

    <div class="FormItem_publishTime__5NEaF">
      <label><input id="opt-immediate" type="radio" name="pub" checked>立即发布</label>
      %SCHEDULE_BLOCK%
    </div>

    <div class="video-list_singlePublish__UqpN1">
      <button id="submit" type="button">发布</button>
    </div>
  </div>

  <!-- 发布确认弹窗（真实结构）：点「发布」不立刻提交，先弹协议，勾了同意才真发 -->
  <div id="agree-modal" class="MDL_outerWrapper_5-180-0 hidden">
    <div class="MDL_inner_5-180-0">
      <label>
        <input id="agree-box" type="checkbox" class="CBX_input_5-180-0" mode="checkbox">
        <div class="CBX_textWrapper_5-180-0">我已阅读并同意</div>
      </label>
      《拼多多短视频管理规范》
      <button id="agree-ok" data-testid="beast-core-modal-ok-button" type="button">
        <span>同意并发布</span>
      </button>
      <button id="agree-cancel" data-testid="beast-core-modal-close-button" type="button">
        <span>不同意</span>
      </button>
    </div>
  </div>

  <div id="toast-area"></div>

<script>
  const $ = (id) => document.getElementById(id);
  const AUTO_FORM = %AUTO_FORM%;
  const STALL_ONCE = %STALL_ONCE%;

  $('draft-cancel').addEventListener('click', () => $('draft-modal').classList.add('hidden'));
  $('draft-continue').addEventListener('click', () => $('draft-modal').classList.add('hidden'));

  function showCard() {
    $('progress').classList.add('hidden');
    if (AUTO_FORM) {
      $('card').classList.remove('hidden');
      // ★ 真实页面在**上传完成**时就会打上这个标签（「视频上传成功」）。
      //   它就是真机上最容易骗到驱动的东西：拿它当"已发布"= 每次上传完就报成功。
      //   假页面照抄过来，谁把判据改回 video-list_success，第 ①⑦ 个场景立刻会挂。
      if (!$('upload-ok')) {
        const ok = document.createElement('div');
        ok.id = 'upload-ok';
        ok.className = 'video-list_success__zjCuV';
        ok.textContent = '视频上传成功';
        $('card').appendChild(ok);
      }
    }
  }

  $('file').addEventListener('change', (e) => {
    window.__file = e.target.files.length ? e.target.files[0].name : '';
    $('progress').classList.remove('hidden');
    // `/stallonce` 变体：**第一次上传永远不完成**（模拟平台侧偶发卡住，实测每 5~8 条会出现一次）。
    // 用 localStorage 记状态：驱动"重新加载页面再传一次"之后，第二次就正常了。
    if (STALL_ONCE && localStorage.getItem('stalled') !== '1') {
      localStorage.setItem('stalled', '1');
      return;   // 卡片永远不出来、进度文案也不再变
    }
    // 模拟"上传 + 转码"：先有进度文案，过一会儿卡片才出来（驱动的等待逻辑吃这个）
    setTimeout(showCard, 1500);
  });

  $('cover-entry').addEventListener('click', () => {
    // 每次打开都回到「视频截取」页签（真实行为），文件框此时不存在
    $('cover-upload-area').classList.add('hidden');
    $('cover-file').value = '';
    $('cover-modal').classList.remove('hidden');
  });
  $('tab-upload').addEventListener('click', () => $('cover-upload-area').classList.remove('hidden'));
  $('cover-cancel').addEventListener('click', () => $('cover-modal').classList.add('hidden'));
  $('cover-confirm').addEventListener('click', () => {
    // ★ 真实平台：没切到「本地上传」/没选图，点「确定」不会产生封面
    const picked = $('cover-file').files.length ? $('cover-file').files[0].name : '';
    if (!picked) { toast('请选择封面'); return; }
    window.__cover = picked;
    // 卡片封面区出现 <img> = 封面设置成功（驱动的回读判据）
    const area = document.querySelector('[class*="video-list_coverImage"]');
    if (area && !area.querySelector('img')) {
      const img = document.createElement('img');
      img.src = 'data:image/gif;base64,R0lGODlhAQABAAAAACw=';
      area.appendChild(img);
    }
    $('cover-modal').classList.add('hidden');
  });

  // 描述编辑器：输入 #话题 时平台会拉起联想浮层，这里也放一个，逼驱动去处理它
  $('desc-editor').addEventListener('input', () => {
    const text = $('desc-editor').innerText || '';
    $('desc-length').textContent = text.length ? String(text.length) : '0';
    if (text.length) $('desc-hack-placeholder').classList.add('hidden');
    if (/#[^\\s#]*$/.test(text)) {
      if (!$('topic-popup')) {
        const popup = document.createElement('div');
        popup.id = 'topic-popup';
        popup.className = 'businessTopic_wrap__nHQRM pdd-toast';
        popup.textContent = '自测话题 热';
        document.body.appendChild(popup);
      }
    } else if ($('topic-popup')) {
      $('topic-popup').remove();
    }
  });
  $('desc-editor').addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && $('topic-popup')) $('topic-popup').remove();
  });

  // ---- 内容声明下拉（真实交互：点 select 展开浮层 → 点选项 → 值写进 input.value）----
  const DECL_OPTIONS = [
    ['内容无需标注', '（作品不含AI生成、虚构、转载及营销等信息）'],
    ['含AI生成内容', ''],
    ['含虚构演绎内容', ''],
    ['内容含营销信息', ''],
    ['内容为转载', ''],
    ['个人观点，仅供参考', ''],
  ];
  document.querySelectorAll('[data-testid="beast-core-select"]').forEach((sel) => {
    sel.addEventListener('click', () => {
      if (document.querySelector('.ST_dropdown_5-180-0')) return;
      const dd = document.createElement('div');
      dd.className = 'ST_dropdown_5-180-0 PP_dropdownMain_5-180-0';
      DECL_OPTIONS.forEach(([label, sub]) => {
        const item = document.createElement('div');
        item.className = 'ContentDeclaration_title__vI1AW';
        item.textContent = label;
        if (sub) {
          const span = document.createElement('span');
          span.className = 'ContentDeclaration_subTitle__9NJLO';
          span.textContent = sub;
          item.appendChild(span);
        }
        // ★ 选项文案带括号说明，但选中后**写进 input 的是不带说明的短名**
        item.addEventListener('click', () => {
          sel.querySelector('input').value = label;
          window.__declaration = label;
          dd.remove();
        });
        dd.appendChild(item);
      });
      document.body.appendChild(dd);
    });
  });

  function toast(text) {
    const el = document.createElement('div');
    el.className = 'pdd-toast';
    el.textContent = text;
    $('toast-area').appendChild(el);
  }

  // ★ 真实平台的两道前置闸门，都照抄：
  //   ① 没选内容声明 → 只弹提示，连请求都不发（第一次真实发布就卡在这）；
  //   ② 点「发布」→ 弹协议窗，**勾了「我已阅读并同意」再点「同意并发布」才真发**（第二次卡在这）。
  async function doPublish() {
    const payload = {
      agreed: $('agree-box').checked,
      declaration: window.__declaration || '',
      file: window.__file || '',
      cover: window.__cover || '',
      desc: $('desc-editor').innerText || '',
      mode: ($('opt-scheduled') && $('opt-scheduled').checked) ? 'scheduled' : 'immediate',
      schedule: $('schedule-time') ? $('schedule-time').value : '',
    };
    // 真实路径：POST /api/backbone/video/publish，按响应决定状态
    const res = await fetch('/api/backbone/video/publish', {
      method: 'POST', body: JSON.stringify(payload),
    });
    const body = await res.json();
    if (body.success === false) {
      // 平台拒稿：弹 toast，卡片**保持原样**（这正是驱动的失败判据）
      toast(body.errorMsg || '发布失败');
      return;
    }
    // 成功：卡片进入已发布状态（按钮文案变「已发布」+ 盖一层 mask）
    $('submit').textContent = '已发布';
    $('submit').disabled = true;
    const mask = document.createElement('div');
    mask.className = 'video-list_mask__oxuJs';
    $('card').appendChild(mask);
  }

  $('submit').addEventListener('click', () => {
    const declSelect = document.querySelector('[data-testid="beast-core-select"]');
    if (declSelect && !window.__declaration) {
      toast('请选择内容声明');
      return;
    }
    // 弹协议窗（不直接发）
    $('agree-modal').classList.remove('hidden');
  });

  $('agree-ok').addEventListener('click', () => {
    // 真实平台：没勾协议点「同意并发布」什么都不会发生
    if (!$('agree-box').checked) {
      toast('请先阅读并同意《拼多多短视频管理规范》');
      return;
    }
    $('agree-modal').classList.add('hidden');
    doPublish();
  });
  $('agree-cancel').addEventListener('click', () => $('agree-modal').classList.add('hidden'));
</script>
</body></html>
"""

SCHEDULE_BLOCK = (
    '<label><input id="opt-scheduled" type="radio" name="pub">定时发布</label>\n'
    '      <input id="schedule-time" type="text" placeholder="请选择时间">'
)


def build_page(*, auto_form: bool = True, schedule: bool = True, draft: bool = True,
               declaration: bool = True, stall_once: bool = False) -> str:
    """按开关拼一张假发布页。"""
    return (
        FAKE_PAGE_TEMPLATE.replace("%AUTO_FORM%", "true" if auto_form else "false")
        .replace("%SCHEDULE_BLOCK%", SCHEDULE_BLOCK if schedule else "")
        .replace("%DRAFT_CLASS%", "" if draft else "hidden")
        .replace("%DECLARATION_CLASS%",
                 "ContentDeclaration_statement__Ue5zP" if declaration else "hidden")
        .replace("%STALL_ONCE%", "true" if stall_once else "false")
    )


class _Handler(BaseHTTPRequestHandler):
    """假发布页 + 发布接口。

    路由就是场景开关（见 `build_page`）：
      `/`              正常页面
      `/noform`        卡片永不出现（上传卡住）
      `/noschedule`    没有「定时发布」选项
      `/nodeclaration` 没有「内容声明」控件（平台会拒发）
      `/stallonce`     第一次上传永远不完成（模拟平台侧偶发卡住，验自动重传）
      `/login`         未登录
      `/coverrequired` 没封面就拒稿（验"自动补封面 + 重试一次"）
    """

    reports: list[dict] = []
    cover_required: bool = False

    def log_message(self, *args):  # 静音，别把自测输出搅乱
        pass

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/login"):
            body = "<html><body>请选择登录身份 我是达人</body></html>"
        elif self.path.startswith("/noform"):
            body = build_page(auto_form=False)
        elif self.path.startswith("/noschedule"):
            body = build_page(schedule=False)
        elif self.path.startswith("/nodeclaration"):
            body = build_page(declaration=False)
        elif self.path.startswith("/stallonce"):
            body = build_page(stall_once=True)
        else:
            body = build_page()
        self._send(body.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).reports.append(payload)

        # 平台的行为：缺封面时回的是 **HTTP 200 + success:false + errorMsg**，不是 HTTP 4xx。
        # ⚠️ 成功信封也要照抄真实结构：`success:true` 的同时 **errorCode 是 1000000（非空）**。
        # 早先的判读写的是"有 errorCode 就算失败"，把两次**已经发布成功**的任务记成了失败——
        # 假服务器回真信封，这条场景才会在回归时把那个 bug 抓回来。
        if self.path.startswith("/api/backbone/video/publish"):
            if type(self).cover_required and not payload.get("cover"):
                body = {"success": False, "errorCode": 400010, "errorMsg": "请添加封面"}
            else:
                body = {
                    "success": True,
                    "errorCode": 1000000,
                    "errorMsg": None,
                    "result": {"pushPxqStatus": 1},
                }
        else:
            body = {"success": True, "errorCode": 1000000, "errorMsg": None}
        self._send(json.dumps(body).encode("utf-8"), "application/json")


def _start_server() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[0], server.server_address[1]
    return server, f"http://{host}:{port}/"


def _make_real_video(path: Path) -> bool:
    """用 ffmpeg 造一个**真能解码**的小视频（竖版 9:16）。

    为什么要真的：场景⑦要验"缺封面 → 驱动自己抽首帧生成合规封面"，
    而抽帧要 ffmpeg 真能解出画面。用假字节流会让那条路径必然失败，等于白测。
    """
    try:
        from uploader.xiaohongshu_uploader.ai_copywriter import find_ffmpeg

        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            return False
        proc = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=steelblue:s=240x426:d=1",
                "-pix_fmt", "yuv420p", str(path),
            ],
            capture_output=True,
            timeout=60,
        )
        return proc.returncode == 0 and path.is_file() and path.stat().st_size > 0
    except Exception:
        return False


def _make_inputs(tmp: Path) -> tuple[str, str, str]:
    """造输入：视频、假 cookie 文件、封面图。"""
    video = tmp / "自测素材.mp4"
    if not _make_real_video(video):
        video.write_bytes(b"\x00" * 2048)  # 兜底：至少让"上传"这一步能跑

    cookie = tmp / "fake_cookie.json"
    cookie.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

    cover = tmp / "cover.jpg"
    try:
        from PIL import Image

        Image.new("RGB", (720, 960), (200, 120, 60)).save(cover)
    except Exception:
        cover.write_bytes(b"\xff\xd8\xff\xd9")
    return str(video), str(cookie), str(cover)


async def _run_case(
    name: str,
    *,
    scheduled: bool,
    with_cover: bool,
    headless: bool,
    path: str = "/",
    upload_timeout: int | None = None,
    cover_required: bool = False,
    retries: int | None = None,
) -> dict:
    """跑一个场景，返回 {"ok": bool, "detail": str, "reports": [...], "steps": [...]}。"""
    server, url = _start_server()
    url = url.rstrip("/") + path
    _Handler.reports = []
    _Handler.cover_required = cover_required

    tmp = Path(tempfile.mkdtemp(prefix="pdd_offline_"))
    video, cookie, cover = _make_inputs(tmp)

    # 把驱动里的目标地址换成假页面。必须改**模块属性**：驱动是
    # `from conf import PDD_PUBLISH_URL` 取的值，改 conf 里那个变量对本进程无效。
    original_url, original_legacy = pdd_main.PDD_PUBLISH_URL, pdd_main.PDD_PUBLISH_URL_LEGACY
    original_stall = pdd_main.PDD_UPLOAD_STALL_SECONDS
    original_retries = pdd_main.PDD_UPLOAD_RETRIES
    pdd_main.PDD_PUBLISH_URL = url
    pdd_main.PDD_PUBLISH_URL_LEGACY = url
    if upload_timeout is not None:
        # 自测里不用真等 120 秒：把"卡住判定"压到几秒
        pdd_main.PDD_UPLOAD_STALL_SECONDS = upload_timeout
    if retries is not None:
        pdd_main.PDD_UPLOAD_RETRIES = retries

    publish_at: datetime | int = datetime.now() + timedelta(hours=5) if scheduled else 0
    strategy = (
        pdd_main.PDD_PUBLISH_STRATEGY_SCHEDULED
        if scheduled
        else pdd_main.PDD_PUBLISH_STRATEGY_IMMEDIATE
    )
    app = pdd_main.PddVideo(
        "自测标题",
        video,
        ["自测话题"],
        publish_at,
        cookie,
        publish_strategy=strategy,
        desc="自测描述",
        thumbnail_path=cover if with_cover else None,
        headless=headless,
    )

    result = {"name": name, "ok": False, "detail": ""}
    try:
        await app.main()
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001 —— 失败也是一种结果，交给调用方判定
        result["detail"] = f"{exc.__class__.__name__}: {exc}"
    finally:
        pdd_main.PDD_PUBLISH_URL, pdd_main.PDD_PUBLISH_URL_LEGACY = original_url, original_legacy
        pdd_main.PDD_UPLOAD_STALL_SECONDS = original_stall
        pdd_main.PDD_UPLOAD_RETRIES = original_retries
        server.shutdown()

    result["reports"] = list(_Handler.reports)
    result["steps"] = [s["step"] for s in app.step_results]
    return result


def _run_publish_center_case(*, headless: bool) -> dict:
    """走**发布中心自己的代码路径**发一条，验证接线而不只是驱动。

    为什么单独做这一条：前面几个场景都是直接构造 `PddVideo` 调 `main()`，
    验的是驱动。而用户实际点的是「发布中心 → 开始发布」，那条路上还有
    `PLATFORMS[5]` / `PUBLISH_STRATEGY[5]` / `_build_tasks`（拼任务、查账号、平台名校验）
    / `_invoke_uploader`（按平台造上传器、传策略）几道接线——任何一处漏了，
    驱动再对也发不出去。

    这里刻意**不**绕开这些函数：只替换"账号表"和"素材目录"两个输入，
    其余全走生产代码。

    必须是**同步**函数、并且在独立线程里跑（调用方用 `asyncio.to_thread`）：
    `_invoke_uploader` 内部会 `asyncio.new_event_loop()` 起自己的循环，
    在已有事件循环里跑会直接报 "Cannot run the event loop while another loop is running"。
    生产里它本来就在 worker 线程里跑，这里也照那样跑。
    """
    import sqlite3 as _sqlite3

    import myUtils.publish_records as records
    import sau_backend_publish as BP

    server, url = _start_server()
    _Handler.reports = []
    _Handler.cover_required = False
    tmp = Path(tempfile.mkdtemp(prefix="pdd_center_"))
    video_src, cookie_src, cover_src = _make_inputs(tmp)

    # 生产代码约定的目录结构：<BASE_DIR>/videoFile/<素材名>、<BASE_DIR>/cookiesFile/<账号文件>
    (tmp / "videoFile").mkdir(exist_ok=True)
    (tmp / "cookiesFile").mkdir(exist_ok=True)
    material = "发布中心素材.mp4"
    shutil.copyfile(video_src, tmp / "videoFile" / material)
    shutil.copyfile(cookie_src, tmp / "cookiesFile" / "pdd.json")
    # 封面本来就在 tmp 里（_make_inputs 的产物），不用、也不能再拷一次
    assert Path(cover_src).is_file()

    db_path = tmp / "database.db"
    result: dict = {"ok": False, "detail": "", "reports": []}
    original_url, original_legacy = pdd_main.PDD_PUBLISH_URL, pdd_main.PDD_PUBLISH_URL_LEGACY
    pdd_main.PDD_PUBLISH_URL = url
    pdd_main.PDD_PUBLISH_URL_LEGACY = url

    with patch.object(records, "DB_PATH", db_path), patch.object(BP, "BASE_DIR", str(tmp)):
        records.ensure_tables()
        with _sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS user_info "
                "(id INTEGER PRIMARY KEY, type INTEGER, filePath TEXT, userName TEXT, status INTEGER)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS file_records "
                "(id INTEGER PRIMARY KEY, file_path TEXT, filename TEXT)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO user_info (id, type, filePath, userName, status) "
                "VALUES (5, 5, 'pdd.json', 'pdd-达人号', 1)"
            )

        # 先让发布中心自己拼任务——这一步会校验账号归属、算定时、写平台名
        _options, tasks = BP._build_tasks(
            {
                "items": [
                    {
                        "material": material,
                        "title": "发布中心标题",
                        "desc": "发布中心正文",
                        "tags": ["发布中心话题"],
                    }
                ],
                "targets": [{"platform": 5, "account_ids": [5]}],
                "options": {},
            }
        )
        task = tasks[0]
        # `_build_tasks` 只拼任务内容；task_id / job_id 是 `publish_submit` 在入队前补的
        # （见 sau_backend_publish.py 里 `task = dict(draft, task_id=uuid.uuid4().hex, …)`）。
        # 这里照它补上，否则 `_invoke_uploader` 拿不到 task_id。
        task["task_id"] = uuid.uuid4().hex
        result["platform_name"] = task["platform_name"]
        result["strategy"] = BP.PUBLISH_STRATEGY[task["platform"]][0]

        # 再走发布中心的执行入口（它会按平台造上传器、把策略传下去）
        try:
            BP._invoke_uploader(task)
            result["ok"] = True
        except Exception as exc:  # noqa: BLE001
            result["detail"] = f"{exc.__class__.__name__}: {exc}"
        finally:
            pdd_main.PDD_PUBLISH_URL, pdd_main.PDD_PUBLISH_URL_LEGACY = original_url, original_legacy
            server.shutdown()

    result["reports"] = list(_Handler.reports)
    result["steps"] = [s.get("step") for s in (task.get("steps") or [])]
    return result


async def main() -> int:
    parser = argparse.ArgumentParser(description="拼多多发布链路离线端到端自测")
    parser.add_argument("--headed", action="store_true", help="有头模式：看着它操作页面")
    args = parser.parse_args()

    headless = not args.headed
    failures: list[str] = []

    def check(condition: bool, label: str) -> None:
        print(("  ✅ " if condition else "  ❌ ") + label)
        if not condition:
            failures.append(label)

    # ---- ① 正常发布（立即）-----------------------------------------------------
    print("\n=== ① 立即发布（草稿弹窗 + 上传等待 + 描述编辑器 + 封面 + 接口回执） ===")
    r1 = await _run_case("立即发布", scheduled=False, with_cover=True, headless=headless)
    check(r1["ok"], f"发布成功（{r1['detail'] or '无异常'}）")
    check(len(r1["reports"]) == 1, f"服务端收到 1 次提交（实际 {len(r1['reports'])} 次）")
    if r1["reports"]:
        rep = r1["reports"][0]
        check(rep["file"] == "自测素材.mp4", f"视频文件名正确（{rep['file']}）")
        check("自测标题" in rep["desc"], f"★ 标题并进了描述（{rep['desc']!r}）")
        check("自测描述" in rep["desc"], "正文也在描述里")
        check("#自测话题" in rep["desc"], "话题写在描述里")
        check(rep["cover"] == "cover.jpg", f"封面已设置（{rep['cover'] or '未设置'}）")
        check(rep["mode"] == "immediate", f"发布方式为立即（{rep['mode']}）")
        check(rep.get("agreed") is True,
              "★ 协议已勾选并点了「同意并发布」（不勾的话平台不会发请求）")
        check(rep.get("declaration") == "内容无需标注",
              f"★ 内容声明已选（{rep.get('declaration')!r}）——平台的必选项，不选会连请求都不发")
    check("submit" in r1["steps"], f"★ 走完了提交步骤（没被「视频上传成功」骗成已发布，{r1['steps']}）")

    # ---- ② 定时发布 ------------------------------------------------------------
    print("\n=== ② 定时发布 ===")
    r2 = await _run_case("定时发布", scheduled=True, with_cover=True, headless=headless)
    check(r2["ok"], f"发布成功（{r2['detail'] or '无异常'}）")
    if r2["reports"]:
        rep = r2["reports"][0]
        check(rep["mode"] == "scheduled", f"确实切到了定时（{rep['mode']}）")
        check(bool(rep["schedule"]), f"定时时间已填（{rep['schedule']}）")
        check("set_schedule" in r2["steps"], f"执行步骤里有定时这一步（{r2['steps']}）")

    # ---- ③ 接口正常回执（④⑦ 的对照组）------------------------------------------
    print("\n=== ③ 接口正常回执（对照组） ===")
    r3 = await _run_case(
        "正常回执", scheduled=False, with_cover=True, headless=headless,
        path="/coverrequired", cover_required=True,
    )
    check(r3["ok"], f"给了封面时正常发布（{r3['detail'] or '无异常'}）")

    # ---- ④ 登录失效 ------------------------------------------------------------
    print("\n=== ④ 登录态失效（发布页跳到 /login） ===")
    r4 = await _run_case("登录失效", scheduled=False, with_cover=True, headless=headless, path="/login")
    check(not r4["ok"], "发布被判为失败（没有硬着头皮往下填）")
    check("重新扫码登录" in r4["detail"], f"错误信息让人知道该去重新登录（{r4['detail'][:60]}）")
    check(not r4["reports"], "没有向平台提交任何东西")

    # ---- ⑤ 上传卡住 ------------------------------------------------------------
    print("\n=== ⑤ 上传卡住（视频卡片一直不出现） ===")
    r5 = await _run_case(
        "上传卡住不重试", scheduled=False, with_cover=True, headless=headless,
        path="/noform", upload_timeout=6, retries=0,
    )
    check(not r5["ok"], "发布被判为失败（没有一直转圈等 10 分钟）")
    check("没有把视频收下" in r5["detail"] or "上传没完成" in r5["detail"],
          f"错误信息说清了是平台侧没接收（{r5['detail'][:70]}）")
    check(not r5["reports"], "没有向平台提交任何东西")

    # ---- ⑥ 定时控件缺失时绝不降级成立即发布 -------------------------------------
    print("\n=== ⑥ 定时发布但页面上没有定时控件 ===")
    r6 = await _run_case(
        "定时控件缺失", scheduled=True, with_cover=True, headless=headless, path="/noschedule"
    )
    check(not r6["ok"], "发布被判为失败")
    check("拒绝降级" in r6["detail"], f"错误信息说清了为什么不发（{r6['detail'][:60]}）")
    check(not r6["reports"], "★ 没有偷偷降级成立即发布（服务端 0 次提交）")

    # ---- ⑦ 缺封面 → 自动补一张合规封面 → 重试成功 -------------------------------
    print("\n=== ⑦ 平台说缺封面 → 自动抽首帧生成合规封面 → 重试一次 ===")
    r7 = await _run_case(
        "自动补封面", scheduled=False, with_cover=False, headless=headless,
        path="/coverrequired", cover_required=True,
    )
    check(r7["ok"], f"没有现成封面也能发布成功（{r7['detail'] or '无异常'}）")
    check("set_cover" in r7["steps"], f"提交前走了设封面这一步（{r7['steps']}）")
    check(len(r7["reports"]) == 1, f"只提交一次（封面在提交前就设好了，实际 {len(r7['reports'])} 次）")
    if r7["reports"]:
        check(bool(r7["reports"][0]["cover"]),
              f"★ 首次提交就带了封面（{r7['reports'][0]['cover']}）——平台不会再弹「请添加封面」")

    # ---- ⑧ 走发布中心自己的代码路径 ---------------------------------------------
    print("\n=== ⑧ 发布中心接线：_build_tasks → _invoke_uploader → 真实驱动 ===")
    r8 = await asyncio.to_thread(_run_publish_center_case, headless=headless)
    check(r8["ok"], f"发布中心路径整体跑通（{r8['detail'] or '无异常'}）")
    check(r8.get("platform_name") == "拼多多视频", f"任务平台名正确（{r8.get('platform_name')}）")
    check(r8.get("strategy") == "immediate", f"策略按立即发布传下去（{r8.get('strategy')}）")
    check(len(r8["reports"]) == 1, f"服务端收到 1 次提交（实际 {len(r8['reports'])} 次）")
    if r8["reports"]:
        check("发布中心标题" in r8["reports"][0]["desc"], "发布中心里的标题落到了描述")

    # ---- ⑨ 页面没有「内容声明」控件 → 平台拒发 → 驱动要把话说清楚 -----------------
    print("\n=== ⑨ 没有「内容声明」控件（平台必选项缺失） ===")
    r9 = await _run_case(
        "缺内容声明", scheduled=False, with_cover=True, headless=headless, path="/nodeclaration"
    )
    check(not r9["ok"], "发布被判为失败（没有报假成功）")
    check("内容声明" in r9["detail"], f"错误信息点名了原因（{r9['detail'][:80]}）")
    check(not r9["reports"], "★ 平台没收到提交（真机行为：只弹提示、不发请求）")

    # ---- ⑩ 第一次上传卡住 → 自动重传后成功 --------------------------------------
    print("\n=== ⑩ 平台侧上传卡住 → 自动重传一次后成功 ===")
    r10 = await _run_case(
        "卡住后重传", scheduled=False, with_cover=True, headless=headless,
        path="/stallonce", upload_timeout=6, retries=1,
    )
    check(r10["ok"], f"重传之后发布成功（{r10['detail'] or '无异常'}）")
    check("upload_video" in r10["steps"], f"上传步骤记成功（{r10['steps']}）")
    check(len(r10["reports"]) == 1, f"最终只提交一次（实际 {len(r10['reports'])} 次）")

    print("\n" + "=" * 64)
    if failures:
        print(f"❌ 自测未通过，{len(failures)} 项不符合预期：")
        for item in failures:
            print(f"   - {item}")
        return 1
    print("✅ 全部 10 个场景通过：正常发布（立即 / 定时）、拒稿、登录失效、上传卡住、")
    print("   「定时控件缺失时绝不降级」、「自动生成封面后再发」、「发布中心接线」，")
    print("   以及「缺内容声明时把原因说清楚」——行为都符合预期")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
