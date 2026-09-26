# -*- coding: utf-8 -*-
"""发布途中「要人搭把手」的输入桥（at-ai-xhs 改造新增）。

有些步骤机器过不去，必须有人看一眼手机——**抖音发布时的短信验证码**是最典型的一个。
上游的做法是：轮询 `BASE_DIR/verify_code.txt`，或者在"有交互终端"时 `input()`。
两条路在 Web 里都走不通：

- Flask 是 `nohup` 起的，stdin 不是终端，`input()` 永远不会有人应答；
- 文件那条更糟：得用户自己猜出"要在项目根目录手写一个 verify_code.txt"，
  什么时候写、写什么、写完有没有生效，界面上一个字都看不到。

所以这里改成**发布线程原地等人**：

1. 上传器 `await request_input(...)`，本模块把这次请求登记进注册表并阻塞；
2. 执行器（`sau_backend_publish._task_prompt`）把请求挂到任务上；
3. 前端轮询到有人在等输入 → 直接弹窗；用户填完
   `POST /publish/prompts/<prompt_id>` → 本模块唤醒等待中的线程；
4. 超时（默认 5 分钟）或用户点「跳过」→ 返回 `None`，
   上传器当作"这次没有验证码"继续往下走，绝不因此把整条发布搞崩。

没有界面时（CLI、脚本、测试）退回终端 `input()`——仍然要求"有人来填"，
但**不再读任何文件**。

线程模型
--------
handler 按**线程**绑定（`threading.local`），和执行器一条任务一条线程的模型一致；
注册表是进程级的（HTTP 请求线程要能按 prompt_id 找到等待中的那个线程）。
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field

# 输入类型。目前只有一种，但接口按"可以再加"来设计：
# 以后要人工确认限额提示、选封面之类，加常量 + 前端按 kind 出控件即可。
KIND_SMS_CODE = "sms_code"

#: 默认等多久算"人不在了"。抖音短信验证码 5 分钟内有效，给足这个时间。
DEFAULT_TIMEOUT = 300.0

_PENDING_STATE = "pending"
_ANSWERED_STATE = "answered"
_SKIPPED_STATE = "skipped"
_EXPIRED_STATE = "expired"


@dataclass
class PromptRequest:
    """一次"等人输入"。等待方阻塞在 `wait()` 上，HTTP 线程调 `answer()`/`skip()` 放行。"""

    id: str
    kind: str
    title: str = ""
    message: str = ""
    placeholder: str = ""
    created_at: float = field(default_factory=time.time)
    timeout: float = DEFAULT_TIMEOUT

    _event: threading.Event = field(default_factory=threading.Event, repr=False)
    _value: str | None = None
    _state: str = _PENDING_STATE
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def age(self) -> float:
        return round(time.time() - self.created_at, 1)

    def to_dict(self) -> dict:
        with self._lock:
            state = self._state
        return {
            "prompt_id": self.id,
            "kind": self.kind,
            "title": self.title,
            "message": self.message,
            "placeholder": self.placeholder,
            "state": state,
            "created_at": self.created_at,
            "age": self.age,
            "timeout": self.timeout,
        }

    def _settle(self, state: str, value: str | None) -> bool:
        """只认第一次结果：晚到的答案（用户手快点了两下）直接丢掉。"""
        with self._lock:
            if self._state != _PENDING_STATE:
                return False
            self._state = state
            self._value = value
        self._event.set()
        return True

    def answer(self, value: str) -> bool:
        return self._settle(_ANSWERED_STATE, (value or "").strip())

    def skip(self) -> bool:
        return self._settle(_SKIPPED_STATE, None)

    def wait(self, timeout: float | None = None) -> str | None:
        """阻塞等待答案；到点返回 None（并把状态标成 expired）。"""
        if not self._event.wait(timeout if timeout is not None else self.timeout):
            self._settle(_EXPIRED_STATE, None)
            return None
        with self._lock:
            return self._value


_local = threading.local()
_registry: dict[str, PromptRequest] = {}
_registry_lock = threading.RLock()


def bind(handler) -> None:
    """把当前线程的"要输入"事件绑到 `handler`（执行器在每个任务线程里调一次）。

    handler 收到的事件：
        {"type": "prompt_open",  "prompt": {...}}
        {"type": "prompt_close", "prompt_id": "..."}
    """
    _local.handler = handler


def unbind() -> None:
    _local.handler = None


def pending_requests() -> list[dict]:
    """当前还在等人的请求（给 HTTP 层列出来用）。"""
    with _registry_lock:
        return [req.to_dict() for req in _registry.values()]


def answer(prompt_id: str, value: str) -> bool:
    """把用户的输入交给等待中的线程。返回 False 表示这个请求已经不在等了。"""
    with _registry_lock:
        req = _registry.get(prompt_id)
    return bool(req and req.answer(value))


def skip(prompt_id: str) -> bool:
    """用户点了「跳过」：不再等这个输入，发布流程照旧往下走。"""
    with _registry_lock:
        req = _registry.get(prompt_id)
    return bool(req and req.skip())


def _notify(handler, event: dict) -> None:
    try:
        handler(event)
    except Exception:
        # 通知失败绝不能影响发布本身
        pass


async def _terminal_input(message: str, timeout: float) -> str | None:
    """没有界面时的兜底：交互终端里直接问。

    刻意**不读任何文件**——"写个 verify_code.txt 等程序来捡"这种约定，
    用的人既看不见也猜不到，出问题时连查都没法查。
    """
    if not sys.stdin or not sys.stdin.isatty():
        return None
    try:
        return (
            await asyncio.wait_for(asyncio.to_thread(input, f"{message}（直接回车跳过）: "), timeout=timeout)
        ).strip() or None
    except (asyncio.TimeoutError, EOFError, OSError):
        return None


async def request_input(
    kind: str,
    *,
    title: str = "",
    message: str = "",
    placeholder: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> str | None:
    """请人来输入一次。返回用户填的内容；没人填（超时/跳过/没有界面）返回 None。"""
    handler = getattr(_local, "handler", None)
    if handler is None:
        return await _terminal_input(message or title or "请输入", timeout)

    req = PromptRequest(
        id=uuid.uuid4().hex,
        kind=kind,
        title=title or "需要你输入",
        message=message,
        placeholder=placeholder,
        timeout=timeout,
    )
    with _registry_lock:
        _registry[req.id] = req
    _notify(handler, {"type": "prompt_open", "prompt": req.to_dict()})
    try:
        # 放到线程里等：事件循环还得继续跑（浏览器交互、日志、其它任务都在上面），
        # 不能为了等一个验证码把整个 loop 堵死。
        return await asyncio.to_thread(req.wait, timeout)
    finally:
        with _registry_lock:
            _registry.pop(req.id, None)
        _notify(handler, {"type": "prompt_close", "prompt_id": req.id})


__all__ = [
    "KIND_SMS_CODE",
    "DEFAULT_TIMEOUT",
    "PromptRequest",
    "answer",
    "bind",
    "pending_requests",
    "request_input",
    "skip",
    "unbind",
]
