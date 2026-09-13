"""极薄的 HTTP 封装，所有外部请求的唯一出口。

不绑定某一个 HTTP 库：MoviePilot 容器里一定有 requests，独立调试时用 httpx，两个都不在才报错。
**不要用 stdlib 的 urllib**：Windows 上它走系统证书库，连 jimaku.cc 会报 CERTIFICATE_VERIFY_FAILED。

所有请求走 `settings.proxy`（插件里默认跟随 MoviePilot 的代理设置）：api.bgm.tv 在国内直连不通，
MoviePilot 自带的 Bangumi 模块也是走代理的。
"""

from __future__ import annotations

import json as _json
from urllib.parse import urlsplit, urlunsplit

from .settings import settings

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
        self.status = status      # None 表示根本没连上（超时、拒绝、代理不通）


# ---------- 代理 ----------

def proxy_url() -> str | None:
    proxy = (settings.proxy or "").strip()
    if not proxy:
        return None
    return proxy if "://" in proxy else f"http://{proxy}"


def describe_proxy() -> str:
    """给页面和日志看的代理说明，隐去账号密码。"""
    proxy = proxy_url()
    if not proxy:
        return "直连"
    parts = urlsplit(proxy)
    netloc = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
    if parts.username:
        netloc = "***@" + netloc
    return urlunsplit((parts.scheme, netloc, "", "", ""))


# ---------- 请求 ----------

def _request(method: str, url: str, *, headers=None, params=None, json=None,
             timeout: float = 30) -> tuple[int, bytes]:
    proxy = proxy_url()
    if _httpx is not None:
        try:
            client = _httpx.Client(timeout=timeout, follow_redirects=True, proxy=proxy)
        except TypeError:                              # httpx < 0.26 的参数名是 proxies
            client = _httpx.Client(timeout=timeout, follow_redirects=True, proxies=proxy)
        with client as c:
            r = c.request(method, url, headers=headers, params=params, json=json)
            return r.status_code, r.content
    r = _requests.request(method, url, headers=headers, params=params, json=json,
                          timeout=timeout, allow_redirects=True,
                          proxies={"http": proxy, "https": proxy} if proxy else None)
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
