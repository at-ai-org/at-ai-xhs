# -*- coding: utf-8 -*-
"""公开仓库里不许出现真实密钥：拿本机 `settings.json` 里的值去比对已入库的文件。

由来（真实事故，2026-10-09）
--------------------------
用户反馈："readme 里面直接显示了明文的密码，这不合适吧"。查下来比"示例写得不好"严重：
README 那段"首次启动会打印这样的密码"的示例，抄的是 `settings.json` 里**正在用**的
`auth.password`，而且是 9d0e0ae 那次入库就带进去的——一个"全站都要登录才能访问"的后台，
密码就这么一直挂在公开仓库的 README 上。`.gitignore` 挡住了文件，**没挡住值**。

这个用例把"抄真实值"变成红灯
----------------------------
做法：从本机 `settings.json` 取敏感字段（后台登录密码 / token 签名密钥 / 模型 API Key），
在**所有已入库的文件**（`git ls-files`，二进制与超大文件跳过）里找一遍，命中即失败。
没有采用"扫所有像密钥的字符串"那种规则：误报一多就没人看了，而这里只跟**确实存在的
真实值**比对，漏报面窄但绝不吵。

只管当前这棵树，不管历史：已经提交过的值改不回来（只能把值本身换掉），别指望它回溯。
本机没有 `settings.json`（CI、容器里就没有）时跳过——没有值可比，不算失败。
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 超过这个大小就不当文本扫了（仓库里最大的已入库文件是 180KB 的 utils/stealth.min.js）
_MAX_BYTES = 4 * 1024 * 1024


def _display(path: Path) -> str:
    """按仓库相对路径显示，临时文件（不在仓库里）就显示绝对路径。"""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def tracked_files() -> list[Path]:
    """已入库的文件。拿不到 git / 不在仓库里就返回空表，由调用方决定跳过。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, timeout=30
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    names = proc.stdout.decode("utf-8", "ignore").split("\0")
    return [ROOT / name for name in names if name]


def real_secrets() -> dict[str, str]:
    """本机 `settings.json` 里"抄出去就等于把后台/额度交出去"的几个值。

    太短的（空串、占位符）不参与比对：拿去扫只会误伤，没有意义。
    """
    path = ROOT / "settings.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    auth = data.get("auth") or {}
    vision = data.get("vision") or {}
    candidates = {
        "auth.password": auth.get("password"),
        "auth.secret_key": auth.get("secret_key"),
        "vision.api_key": vision.get("api_key"),
    }
    return {key: str(val) for key, val in candidates.items() if val and len(str(val)) >= 8}


def scan_for_secrets(paths, secrets: dict[str, str]) -> list[str]:
    """在这些文件里找这些值，返回 `文件:行号（字段）`。与"真实值"解耦，便于自测。"""
    hits: list[str] = []
    for path in paths:
        try:
            if not path.is_file() or path.stat().st_size > _MAX_BYTES:
                continue
            raw = path.read_bytes()
        except Exception:
            continue
        if b"\x00" in raw[:8192]:  # 二进制（图片、字体…）不当文本看
            continue
        text = raw.decode("utf-8", "ignore")
        for field, secret in secrets.items():
            if not secret or secret not in text:
                continue
            line = text[: text.index(secret)].count("\n") + 1  # 指到真正那一行
            hits.append(f"{_display(path)}:{line}（{field}）")
    return hits


class ScanForSecretsTest(unittest.TestCase):
    """先证扫描器本身有效——否则"没扫到"可能只是扫描器坏了。"""

    def test_catches_a_planted_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            planted = Path(tmp) / "README.md"
            planted.write_text("占位一行\n密码：S3cret-Plant3d-Value\n", encoding="utf-8")

            hits = scan_for_secrets([planted], {"auth.password": "S3cret-Plant3d-Value"})
            self.assertEqual(len(hits), 1, hits)
            self.assertIn(":2", hits[0], "行号要指到真正那一行")
            self.assertIn("auth.password", hits[0])

            self.assertEqual(scan_for_secrets([planted], {"auth.password": "别的值"}), [])

    def test_skips_binary_and_oversized_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            blob = Path(tmp) / "logo.png"
            blob.write_bytes(b"\x89PNG\x00\x00S3cret-Plant3d-Value")

            self.assertEqual(
                scan_for_secrets([blob], {"auth.password": "S3cret-Plant3d-Value"}),
                [],
                "二进制里碰巧出现的字节串不算泄漏",
            )


class NoSecretInRepoTest(unittest.TestCase):
    """真正的守卫：本机正在用的那几个值，一个都不许出现在已入库的文件里。"""

    def test_no_real_secret_in_tracked_files(self):
        secrets = real_secrets()
        if not secrets:
            self.skipTest("本机没有 settings.json（或里面没有可供比对的值）")

        files = tracked_files()
        if not files:
            self.skipTest("拿不到 git ls-files（不在 git 仓库里？）")

        hits = scan_for_secrets(files, secrets)
        self.assertEqual(
            hits,
            [],
            "这些**已入库**的文件里出现了本机 settings.json 的真实值，"
            "等于把后台登录密码 / API 额度一起公开：\n  "
            + "\n  ".join(hits)
            + "\n示例一律写占位符；如果值已经提交过，改文件没用，只能把这个值本身换掉。",
        )
