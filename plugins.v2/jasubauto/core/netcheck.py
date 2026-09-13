"""网络自检：从插件所在的容器里，挨个试连要用到的外部站点。

用户在页面上点一下就知道是不是网络问题、代理有没有生效，不必翻日志猜。
"""

from __future__ import annotations

import time

from . import bangumi, http

TARGETS = (
    ("Jimaku", "https://jimaku.cc/", "下载字幕"),
    ("Bangumi", "https://api.bgm.tv/v0/subjects/400602", "识别只有 Bangumi ID 的剧"),
    ("AniList", "https://graphql.anilist.co/", "核对开播年份"),
    ("GitHub", "https://raw.githubusercontent.com/Fribb/anime-lists/master/README.md", "下载 ID 映射表"),
)


def run() -> dict:
    results = []
    for name, url, purpose in TARGETS:
        http.forget(url)                     # 自检要真的去连，不能被"刚才连不上"的记录挡掉
        start = time.monotonic()
        try:
            http.get_bytes(url, headers={"User-Agent": bangumi.UA}, timeout=15)
            ok, detail = True, "正常"
        except http.HttpError as exc:
            ok = exc.status is not None      # 返回了状态码就说明连得上
            detail = f"HTTP {exc.status}（连得上）" if ok else str(exc)
        results.append({"name": name, "purpose": purpose, "ok": ok, "detail": detail,
                        "seconds": round(time.monotonic() - start, 1)})
    return {"proxy": http.describe_proxy(), "results": results}
