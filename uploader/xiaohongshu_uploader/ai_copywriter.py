# -*- coding: utf-8 -*-
"""AI 文案生成（at-ai-xhs 改造新增）。

流程：抽视频**首帧 + 尾帧** → 交给 OpenAI 兼容的多模态模型 → 直接产出小红书文案。

为什么只看首尾两帧
------------------
- 首帧通常决定"点不点进来"（封面感、主体、场景），尾帧通常能反映"看完得到什么"；
- 只发 2 张图，token 成本极低、响应快，且对绝大多数笔记已经够用；
- 需要更细的理解时再扩展成多帧采样，接口不用变。

为什么把**素材文件名**也发过去
------------------------------
两帧画面能看出"画面里有什么"，但看不出"这条视频按什么口径在讲"：同一条海景视频，
素材叫「2026-10-09 青岛日落」和叫「随手拍的」写出来的文案天差地别——用户起文件名时
往往已经把主题/时间/地点/品类写进去了，这是零成本的补充信息，浪费掉很可惜
（用户诉求："素材生成文案时要把素材的文件名称考虑进去"）。

它是**弱线索**：画面优先，名字和画面对不上就不采纳（系统提示词里写明了），
免得为了迁就一个过期的文件名去编造。文件名经 `material_name_of` 清洗后再发
（去扩展名、去 uuid 前缀、去换行/控制字符、限长），不会把路径或脏字符带进提示词。

例外：**上一版 AI 标题改出来的文件名不当线索**。生成文案后素材会被改名成标题，
再喂回去等于拿上一版答案当提示——用户点「重新生成」只会得到同一份文案。
判断在调用方（`sau_backend_xhs._material_hint_name`），这里只管"给不给"。

只用标准 `requests` 调 `/chat/completions`，不引入任何厂商 SDK——
这样任何"OpenAI 格式"的服务（官方、中转、vLLM、Ollama 的兼容层…）都能直接填 URL/Key/模型名用。
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests

from myUtils.app_settings import get_vision_config

# 抽帧输出宽度：720 已经足够模型看懂画面，又能显著省 token
FRAME_MAX_WIDTH = 720

# 正文默认字数上限。做成设置项了，可以在「模型设置」页调，不用改代码。
DEFAULT_DESC_MAX_CHARS = 100

#: 喂给模型的素材文件名长度上限。
#:
#: 文件名只是线索：相机/手机导出的一长串编号（`VID_20261009_183012_1_1_1`）没什么信息量，
#: 却会实打实占 token。截断到 60 字足够装下"日期 + 主题"这种真有用的名字。
MATERIAL_NAME_MAX_CHARS = 60

#: 上传时拼在磁盘名前面的 uuid 前缀（`<uuid>_原名.mp4`，见 `sau_backend.py` 的 `/upload`）。
#: 界面上的「文件名」列不带它，喂给模型时也不该带——一串十六进制对模型只有干扰。
_UUID_PREFIX_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}[_-]"
)


def material_name_of(video_path: str | Path, max_chars: int = MATERIAL_NAME_MAX_CHARS) -> str:
    """素材文件（名/路径）→ 可以喂给模型的「参考名」。

    只留下模型用得上的那部分：

    * 取 basename——绝对路径对模型是噪音，也不该把用户的目录结构发出去；
    * 去掉扩展名（`.mp4` 没有信息量）；
    * 去掉上传时加的 uuid 前缀，还原成用户在「素材管理」里看到的名字；
    * 换行/制表/控制字符换成空格再收敛——文件名是**外部输入**，一个带换行的名字
      本身就能在提示词里"另起一行"，这类字符一律先清掉；
    * 超长截断。

    洗不出东西（空文件名、只剩一串分隔符之类）就返回空串，调用方据此不发这一段。
    """
    name = Path(str(video_path or "")).name
    name = _UUID_PREFIX_RE.sub("", name)
    # 只在最后一段"看起来像扩展名"时才切（`_safe_upload_name` 用的是同一个判据）。
    # 用裸 `Path.stem` 会把 `v1.2 标题` 切成 `v1`——文件名叫这个完全合法，
    # 而且改名后的素材名里允许出现 `.` 之外的任何字符，不该在这里丢信息。
    suffix = Path(name).suffix
    stem = name[: -len(suffix)] if re.fullmatch(r"\.[A-Za-z0-9]{1,6}", suffix or "") else name
    stem = re.sub(r"[\x00-\x1f\x7f]", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem[:max_chars].strip()


def build_system_prompt(max_chars: int = DEFAULT_DESC_MAX_CHARS) -> str:
    """按字数上限生成系统提示词——限制变了，措辞也跟着变，不用两处维护。"""
    return f"""你是资深小红书内容运营。你会看到一条短视频的开头和结尾两处画面，
请判断这条视频在讲什么，并写出可以直接发布的小红书笔记文案。

**整体语气：客观、克制、信息导向。** 像在介绍一件东西、讲清一件事，
而不是在抒发"我看完之后的感受"。

硬性要求：
1. 只输出 JSON，不要任何解释、不要 markdown 代码块；
2. title 不超过 20 个字：说清这条视频"是什么 / 有什么看点"，可以吸引人，
   但不要靠夸张情绪词；
3. tags 是话题标签数组，**只要 3 到 4 个**，不要带 # 号，与内容强相关，宁缺毋滥；
4. desc 是正文，**硬性上限 {max_chars} 个字左右**，超了就算不合格。因为篇幅很短：
   - 一段话写完，不要分小标题、不要列长清单、不要分点；
   - 直接说清"这是什么 + 有什么看点或用途"，把次要细节全部舍弃；
   - 保持客观陈述，不要情绪表达：避免"谁懂啊""绝了""太治愈""上头""绷不住"
     "真的会谢""泪目""心动""挪不开眼"这类词，也不要写"让我想起…"式抒情；
   - emoji 最多 1 个，也可以不用；
5. **正文里绝对不要出现任何暴露素材来源或创作过程的表达**，例如"第一帧""尾帧"
   "首帧""画面""图片""截图""这两张图""从画面可以看出""根据提供的图片"等。
   读者不应该感觉到任何"分析"痕迹；
6. 只写你能确定的信息。看不清的（价格、地点、品牌、具体数字）一律不要编造；
7. 如果消息里带了**素材文件名**（用户自己给这条素材起的名字），把它当作理解内容的线索：
   它常常写着主题、时间、地点、品类，能对上画面就用上，让文案更贴题；
   但**画面永远优先**——名字和画面对不上时以画面为准，也不要为了迁就名字去编造，
   更不要把文件名原样照抄成标题。

输出 JSON 结构（**字段顺序必须照抄：tags 写在 desc 前面**）：
{{"title": "...", "tags": ["...", "...", "..."], "desc": "..."}}"""


SYSTEM_PROMPT = build_system_prompt()


# --------------------------------------------------------------------------- #
# 抽帧
# --------------------------------------------------------------------------- #


def find_ffmpeg() -> str | None:
    """找一个可用的 ffmpeg：优先 imageio-ffmpeg 自带的静态二进制，其次 PATH。"""
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return exe
    except Exception:
        pass
    return shutil.which("ffmpeg")


def _run_ffmpeg(ffmpeg: str, args: list[str], timeout: int = 120) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", *args],
            capture_output=True,
            timeout=timeout,
        )
        return proc.returncode == 0, (proc.stderr or b"").decode("utf-8", "ignore").strip()
    except Exception as exc:
        return False, str(exc)


def extract_key_frames(video_path: str | Path, out_dir: str | Path | None = None) -> dict:
    """抽取首帧与尾帧，返回 {"first": path|None, "last": path|None, "errors": [...]}。

    尾帧抽取在短视频/异常文件上可能失败，失败不影响首帧可用——按"能拿到多少用多少"处理。
    """
    video = Path(video_path)
    result: dict = {"first": None, "last": None, "errors": []}
    if not video.is_file():
        result["errors"].append(f"视频不存在: {video}")
        return result

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        result["errors"].append("找不到 ffmpeg（可 pip install imageio-ffmpeg 自带的静态二进制）")
        return result

    work = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="xhs_frames_"))
    work.mkdir(parents=True, exist_ok=True)
    scale = f"scale='min({FRAME_MAX_WIDTH},iw)':-2"

    first = work / "first.jpg"
    ok, err = _run_ffmpeg(ffmpeg, ["-y", "-ss", "0", "-i", str(video), "-frames:v", "1", "-vf", scale, "-q:v", "3", str(first)])
    if not (ok and first.is_file() and first.stat().st_size > 0):
        # 静态图（素材库里的 jpg/png）必然走到这里：`-ss 0` 在 `-i` **之前**时 ffmpeg 会先
        # 定位再解码，而图片只有一帧，定位完就没帧可编了（报 "Output file is empty"）。
        # 去掉 `-ss` 再试一次——图片无所谓定位，顺带也兜住时长极短的视频。
        # 只在**第一次已经失败**时才重试，所以对原来能跑的视频没有任何行为变化。
        ok, err = _run_ffmpeg(ffmpeg, ["-y", "-i", str(video), "-frames:v", "1", "-vf", scale, "-q:v", "3", str(first)])
    if ok and first.is_file() and first.stat().st_size > 0:
        result["first"] = str(first)
    else:
        result["errors"].append(f"抽取首帧失败: {err}")

    # 尾帧：-sseof 从文件末尾往前定位；不同容器/时长下偏移量需要退让重试
    for offset in ("-1", "-0.3", "-0.05"):
        last = work / "last.jpg"
        ok, err = _run_ffmpeg(
            ffmpeg,
            ["-y", "-sseof", offset, "-i", str(video), "-frames:v", "1", "-vf", scale, "-q:v", "3", str(last)],
        )
        if ok and last.is_file() and last.stat().st_size > 0:
            # 极端情况下末尾这一帧和首帧是同一张，那就没必要发两张
            if result["first"] and last.read_bytes() == Path(result["first"]).read_bytes():
                result["errors"].append("尾帧与首帧相同，仅使用首帧")
                break
            result["last"] = str(last)
            break
    else:
        result["errors"].append("抽取尾帧失败，仅使用首帧")

    return result


def _to_data_url(path: str) -> str:
    b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


# --------------------------------------------------------------------------- #
# 调用模型
# --------------------------------------------------------------------------- #


def chat_completion(messages: list[dict], *, timeout: int | None = None, max_tokens: int | None = None) -> str:
    """调一次 OpenAI 兼容的 /chat/completions，返回文本内容。"""
    cfg = get_vision_config()
    base_url = (cfg.get("base_url") or "").strip().rstrip("/")
    api_key = (cfg.get("api_key") or "").strip()
    model = (cfg.get("model") or "").strip()

    if not base_url:
        raise RuntimeError("还没有配置模型服务地址（base_url），请到「模型设置」页填写")
    if not model:
        raise RuntimeError("还没有配置模型名（model），请到「模型设置」页填写")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    if max_tokens is None:
        max_tokens = int(cfg.get("max_tokens") or 8000)
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
    resp = requests.post(
        f"{base_url}/chat/completions",
        headers=headers,
        json=payload,
        timeout=timeout or int(cfg.get("timeout") or 120),
    )
    if resp.status_code >= 400:
        # 把服务端返回体带出来，配置错了能一眼看出原因（401/404/模型名不存在…）
        raise RuntimeError(f"模型接口返回 {resp.status_code}: {resp.text[:400]}")

    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"模型返回结构异常: {json.dumps(data, ensure_ascii=False)[:400]}")


def _salvage_partial(text: str) -> dict:
    """从被截断的 JSON 里尽量抢救出字段。

    实测：模型偶发会因为 max_tokens 用尽被截断（中文 600 字正文可能超过 1200 token），
    此时严格解析必然失败，但 title 和大部分 desc 其实已经拿到了，直接丢弃很浪费。
    """
    out: dict = {}
    for key in ("title", "desc", "content"):
        m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if m:
            try:
                out[key] = json.loads(f'"{m.group(1)}"')
            except Exception:
                out[key] = m.group(1)
        else:
            # 未闭合的字符串（正好被截断在正文中间）
            m = re.search(rf'"{key}"\s*:\s*"(.*)$', text, re.S)
            if m:
                frag = m.group(1).replace("\\n", "\n").replace('\\"', '"').rstrip('\\" ')
                if frag:
                    out[key] = frag
    tags = re.findall(r'"((?:[^"\\]|\\.)*)"', text.split('"tags"')[-1]) if '"tags"' in text else []
    if tags:
        out["tags"] = [t for t in tags if t not in ("tags", "title", "desc")][:4]
    return out


def _parse_json_block(text: str) -> dict:
    """从模型输出里抠出 JSON（容忍 ```json 代码块和前后废话）。"""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            pass
    # 最后一道防线：抢救被截断的响应
    salvaged = _salvage_partial(text)
    if salvaged.get("title"):
        return salvaged
    raise RuntimeError(f"模型没有返回可解析的 JSON: {text[:300]}")


def _generate_tags_only(title: str, desc: str) -> list[str]:
    """话题兜底：万一主请求被截断没吐出 tags，单独补一次很小的请求。

    只发文本、不发图片，成本极低；比"话题为空"这种残缺结果好得多。
    """
    try:
        text = chat_completion(
            [
                {"role": "system", "content": "你只输出 JSON 数组，不要任何解释和代码块。"},
                {
                    "role": "user",
                    "content": (
                        "给下面这条小红书笔记写 3 到 4 个话题标签，不带 # 号，与内容强相关。\n"
                        f"标题：{title}\n正文：{desc[:400]}\n"
                        '只输出形如 ["标签1","标签2","标签3"] 的 JSON 数组。'
                    ),
                },
            ],
            max_tokens=300,
        )
        m = re.search(r"\[([\s\S]*?)\]", text or "")
        if not m:
            return []
        items = json.loads("[" + m.group(1) + "]")
        return [str(x).strip().lstrip("#") for x in items if str(x).strip()][:4]
    except Exception:
        return []


def _clamp_desc(desc: str, max_chars: int) -> str:
    """把正文压到上限内。

    提示词只是"请求"模型守规矩，模型偶尔会超——这里做硬保证。
    截断时优先切在句末标点上，避免出现半句话。
    """
    desc = (desc or "").strip()
    if max_chars <= 0 or len(desc) <= max_chars:
        return desc
    head = desc[:max_chars]
    for punct in ("。", "！", "？", "…", "\n", ".", "!", "?"):
        idx = head.rfind(punct)
        # 切点太靠前就干脆不切（宁可略超，也不要只剩半句）
        if idx >= max_chars * 0.6:
            return head[: idx + 1].strip()
    return head.rstrip() + "…"


def generate_note_copy(
    video_path: str | Path,
    hints: str = "",
    desc_max_chars: int | None = None,
    material_name: str | None = None,
) -> dict:
    """抽首尾帧 → 让多模态模型产出 {title, desc, tags}。

    Args:
        video_path: 素材文件（也是文件名线索的默认来源）。
        hints: 用户在界面上填的补充信息。
        desc_max_chars: 正文上限，默认读设置。
        material_name: 发给模型的**素材文件名线索**。
            `None`（默认）= 从 `video_path` 的文件名推（见 `material_name_of`），
            这也是"生成文案要把文件名考虑进去"的落点；
            传空串 = 明确不带（例如文件名就是上一版 AI 标题，见模块说明）。

    Returns:
        dict: {"ok": bool, "title", "desc", "tags", "frames", "model", "raw", "error"}
    """
    cfg = get_vision_config()
    if desc_max_chars is None:
        desc_max_chars = int(cfg.get("desc_max_chars") or DEFAULT_DESC_MAX_CHARS)
    if material_name is None:
        material_name = material_name_of(video_path)
    material_name = (material_name or "").strip()
    out: dict = {
        "ok": False,
        "title": "",
        "desc": "",
        "tags": [],
        "frames": {},
        "model": cfg.get("model"),
        "raw": "",
        "desc_max_chars": desc_max_chars,
        "material_name": material_name,
    }

    frames = extract_key_frames(video_path)
    out["frames"] = {
        "first": bool(frames.get("first")),
        "last": bool(frames.get("last")),
        "warnings": frames.get("errors") or [],
    }
    if not frames.get("first"):
        out["error"] = "；".join(frames.get("errors") or ["抽帧失败"])
        return out

    content: list[dict] = [
        {"type": "text", "text": "这是这条视频的画面参考（开头和结尾各一张），请据此写小红书笔记文案。"}
    ]
    content.append({"type": "image_url", "image_url": {"url": _to_data_url(frames["first"])}})
    if frames.get("last"):
        content.append({"type": "image_url", "image_url": {"url": _to_data_url(frames["last"])}})
    if material_name:
        # 放在图后面：先看画面，再拿到"用户是怎么称呼这条素材的"这条线索，
        # 模型不至于被一个过期的名字先入为主。
        content.append(
            {"type": "text", "text": f"素材文件名：{material_name}（用户自己起的，仅作线索）"}
        )
    extra = (cfg.get("extra_prompt") or "").strip()
    if extra:
        content.append({"type": "text", "text": f"额外要求：{extra}"})
    if hints:
        content.append({"type": "text", "text": f"补充信息（请结合使用）：{hints}"})

    messages = [
        {"role": "system", "content": build_system_prompt(desc_max_chars)},
        {"role": "user", "content": content},
    ]

    try:
        raw = chat_completion(messages)
    except Exception as exc:
        out["error"] = str(exc)
        return out

    out["raw"] = raw
    try:
        parsed = _parse_json_block(raw)
    except Exception as exc:
        out["error"] = str(exc)
        return out

    title = str(parsed.get("title") or "").strip()[:20]
    desc = _clamp_desc(str(parsed.get("desc") or parsed.get("content") or ""), desc_max_chars)
    # 话题只要 3-4 个：宁缺毋滥，多了反而分散流量
    tags = [str(t).strip().lstrip("#") for t in (parsed.get("tags") or []) if str(t).strip()][:4]

    if not title:
        out["error"] = "模型返回的标题为空"
        return out

    # 话题为空通常意味着响应被截断（推理模型思考过程吃掉了额度），补一次小请求
    if not tags and desc:
        tags = _generate_tags_only(title, desc)
        if tags:
            out["frames"]["warnings"] = (out["frames"].get("warnings") or []) + ["话题由兜底请求补全"]

    out.update({"ok": True, "title": title, "desc": desc, "tags": tags})
    return out
