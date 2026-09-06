"""极薄的 HTTP 封装。

存在的唯一理由是让核心逻辑不绑定任何一个 HTTP 库：
  * MoviePilot 容器里一定有 requests（它自己在用）
  * 独立调试时用 httpx
两个都不在才报错。**不要用 stdlib 的 urllib**：Windows 上它走系统证书库，
连 jimaku.cc 会报 CERTIFICATE_VERIFY_FAILED（证书过期），httpx/requests 走 certifi 则正常。
"""

from __future__ import annotations

import json as _json

try:                                    # 优先 httpx
    import httpx as _httpx
except ImportError:                     # pragma: no cover
    _httpx = None
try:
    import requests as _requests
except ImportError:                     # pragma: no cover
    _requests = None

if _httpx is None and _requests is None:  # pragma: no cover
    raise ImportError("需要 httpx 或 requests 之一")


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _request(method: str, url: str, *, headers=None, params=None, json=None,
             timeout: float = 30) -> tuple[int, bytes]:
    if _httpx is not None:
        with _httpx.Client(timeout=timeout, follow_redirects=True) as c:
            r = c.request(method, url, headers=headers, params=params, json=json)
            return r.status_code, r.content
    r = _requests.request(method, url, headers=headers, params=params, json=json,
                          timeout=timeout, allow_redirects=True)
    return r.status_code, r.content


def get_bytes(url: str, *, headers=None, params=None, timeout: float = 30) -> bytes:
    status, body = _request("GET", url, headers=headers, params=params, timeout=timeout)
    if status >= 400:
        raise HttpError(f"GET {url} 返回 {status}", status)
    return body


def get_json(url: str, *, headers=None, params=None, timeout: float = 30):
    return _json.loads(get_bytes(url, headers=headers, params=params, timeout=timeout))


def post_json(url: str, payload: dict, *, headers=None, timeout: float = 30):
    status, body = _request("POST", url, headers=headers, json=payload, timeout=timeout)
    if status >= 400:
        raise HttpError(f"POST {url} 返回 {status}", status)
    return _json.loads(body)
