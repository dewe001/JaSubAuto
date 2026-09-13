"""识别过程记录：每一步判断、每次外部请求的结果（含报错原因）。

手动页面逐集展示，同时写进 MoviePilot 日志——要能回答"这一集为什么没下到字幕"。
用 contextvars 收集：处理每一集时包一层 `capture()`，期间所有 `log()` 都记到这一集名下。
"""

from __future__ import annotations

import contextvars
from typing import Callable

_lines: contextvars.ContextVar = contextvars.ContextVar("jasubauto_trace", default=None)
_sink: Callable[[str], None] | None = None


def set_sink(fn: Callable[[str], None] | None) -> None:
    """插件里设成 MoviePilot 的 logger，调试壳不设。"""
    global _sink
    _sink = fn


def log(message: str) -> None:
    lines = _lines.get()
    if lines is not None:
        lines.append(message)
    if _sink is not None:
        try:
            _sink(message)
        except Exception:
            pass


class capture:
    """`with trace.capture() as lines:` 期间的 `log()` 都进 `lines`。"""

    def __enter__(self) -> list[str]:
        self.lines: list[str] = []
        self._token = _lines.set(self.lines)
        return self.lines

    def __exit__(self, *exc) -> None:
        _lines.reset(self._token)
