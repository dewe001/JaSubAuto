"""Jimaku API 客户端。

接口约定（已对实际接口验证过）：
  GET /api/entries/search?anilist_id=<id>  -> [{id, name, flags{...}, anilist_id, english_name, japanese_name, last_modified}]
  GET /api/entries/<entry_id>/files        -> [{url, name, size, last_modified}]
认证：Authorization: <token>，**不带 Bearer 前缀**。限速 25 请求/分钟。
"""

from __future__ import annotations

import threading
import time
from collections import deque

from . import http
from .settings import settings

BASE = "https://jimaku.cc/api"
UA = "JaSubAuto (+https://github.com/dewe001/JaSubAuto)"

RATE_LIMIT = 25          # 官方限制：25 请求/分钟
RATE_WINDOW = 60.0
SAFETY_MARGIN = 1.0      # 留一点余量，避免边界抖动被限流


class RateLimiter:
    """滑动窗口限速器。所有 Jimaku 调用都必须经过它。"""

    def __init__(self, limit: int = RATE_LIMIT, window: float = RATE_WINDOW):
        self._limit = limit
        self._window = window
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] > self._window:
                self._calls.popleft()
            if len(self._calls) >= self._limit:
                sleep_for = self._window - (now - self._calls[0]) + SAFETY_MARGIN
                time.sleep(max(sleep_for, 0))
                now = time.monotonic()
                while self._calls and now - self._calls[0] > self._window:
                    self._calls.popleft()
            self._calls.append(time.monotonic())


_limiter = RateLimiter()


class JimakuError(RuntimeError):
    pass


def _headers() -> dict:
    if not settings.jimaku_api_token:
        raise JimakuError("没配置 Jimaku API Token")
    return {
        "Authorization": settings.jimaku_api_token,   # 注意：不加 Bearer
        "Accept": "application/json",
        "User-Agent": UA,
    }


def _get(path: str, **params) -> list[dict]:
    _limiter.acquire()
    try:
        data = http.get_json(f"{BASE}{path}", headers=_headers(), params=params or None)
    except http.HttpError as exc:
        if exc.status == 429:
            raise JimakuError("触发 Jimaku 限流（429），请降低调用频率") from exc
        raise JimakuError(str(exc)) from exc
    return data if isinstance(data, list) else [data]


def search_entries(anilist_id: int) -> list[dict]:
    """按 anilist_id 查字幕条目。"""
    return _get("/entries/search", anilist_id=anilist_id)


def search_by_title(query: str) -> list[dict]:
    """按标题查条目。**是模糊搜索**：「Dungeon Meshi」会带出三部「ダンジョンに出会い…」，
    调用方必须自己拿 name / japanese_name 做全等比对，不能取第一条。"""
    return _get("/entries/search", query=query)


def list_files(entry_id: int) -> list[dict]:
    """列出某条目下的全部字幕文件。"""
    return _get(f"/entries/{entry_id}/files")


def download(url: str) -> bytes:
    """下载单个字幕文件。"""
    _limiter.acquire()
    return http.get_bytes(url, headers=_headers())
