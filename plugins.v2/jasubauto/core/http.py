"""极薄的 HTTP 封装，所有外部请求的唯一出口。

不绑定某一个 HTTP 库：MoviePilot 容器里一定有 requests，独立调试时用 httpx，两个都不在才报错。
**不要用 stdlib 的 urllib**：Windows 上它走系统证书库，连 jimaku.cc 会报 CERTIFICATE_VERIFY_FAILED。

放在这一层、所有请求自动享有的两件事：
  * 代理：`settings.proxy`（插件里默认跟随 MoviePilot 的代理设置）。api.bgm.tv 在国内直连不通
  * 连不上就快速放弃：连接超时 10 秒；某个站连不上后 5 分钟内直接跳过，不再每集等一次超时
"""

from __future__ import annotations

import json as _json
import time
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

CONNECT_TIMEOUT = 10        # 连不上的站，等 10 秒足够判断
DOWN_SECONDS = 300          # 连不上之后这么久之内不再尝试


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


# ---------- 连不上的站暂时跳过 ----------

_down: dict[str, tuple[float, str]] = {}      # host -> (这个时间点之前不再尝试, 原因)


def reset() -> None:
    _down.clear()


def _host(url_or_host: str) -> str:
    return (urlsplit(url_or_host).hostname or url_or_host) if "://" in url_or_host else url_or_host


def host_problem(url_or_host: str) -> str:
    """这个站最近连不上的原因，没问题返回空串。上层拼进"为什么没识别"的说明里。"""
    hit = _down.get(_host(url_or_host))
    return hit[1] if hit and time.time() < hit[0] else ""


def forget(url_or_host: str) -> None:
    """清掉某个站"连不上"的记录（网络自检要真的去连）。"""
    _down.pop(_host(url_or_host), None)


# ---------- 请求 ----------

def _send(method, url, *, headers, params, json, timeout, proxy) -> tuple[int, bytes]:
    if _httpx is not None:
        options = dict(timeout=_httpx.Timeout(timeout, connect=CONNECT_TIMEOUT), follow_redirects=True)
        try:
            client = _httpx.Client(proxy=proxy, **options)
        except TypeError:                              # httpx < 0.26 的参数名是 proxies
            client = _httpx.Client(proxies=proxy, **options)
        with client as c:
            r = c.request(method, url, headers=headers, params=params, json=json)
            return r.status_code, r.content
    r = _requests.request(method, url, headers=headers, params=params, json=json,
                          timeout=(CONNECT_TIMEOUT, timeout), allow_redirects=True,
                          proxies={"http": proxy, "https": proxy} if proxy else None)
    return r.status_code, r.content


def _request(method: str, url: str, *, headers=None, params=None, json=None,
             timeout: float = 30) -> tuple[int, bytes]:
    host = _host(url)
    path = urlsplit(url).path
    label = f"{method} {host}{path if len(path) <= 60 else path[:57] + '...'}"
    problem = host_problem(host)
    if problem:
        raise HttpError(f"{host} 连不上：{problem}")

    proxy = proxy_url()
    start = time.monotonic()
    try:
        status, body = _send(method, url, headers=headers, params=params, json=json,
                             timeout=timeout, proxy=proxy)
    except Exception as exc:
        detail = str(exc).strip() or type(exc).__name__
        via = f"经代理 {describe_proxy()}" if proxy else "直连"
        reason = f"{type(exc).__name__}（{via}）：{detail[:120]}"
        _down[host] = (time.time() + DOWN_SECONDS, reason)
        raise HttpError(f"{host} 连不上：{reason}") from exc
    return status, body


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
