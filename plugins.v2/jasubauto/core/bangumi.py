"""Bangumi（bgm.tv）：给只有 Bangumi ID 的剧找到具体条目和集号。

为什么需要：用 Jellyfin 的 Bangumi 插件刮削的库，nfo 里常常只有 `<bangumiid>`，没有 tmdb。
这里负责三件事：
  * 取条目信息（日文原名、别名、开播日期）、正片剧集列表、续集关系——都走 api.bgm.tv
  * 读 BangumiExtLinker 的离线映射表：bgm_id → MAL / AniDB ID（identify 再接到 AniList）
  * 把媒体库里的集号定位到"哪个 Bangumi 条目的第几集"（`locate`）

Bangumi 的分季和 AniList 一致（一季一个条目），但每一集有两个编号：
  ep   = 条目内第几集，AniList 用的就是这个
  sort = 跨季连续编号。咒术回战第二季第 1 集是 sort=25, ep=1
所有网络调用失败都返回 None，由调用方交人工，绝不抛异常。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from . import http, trace
from .settings import settings

API = "https://api.bgm.tv"
MAP_URL = "https://raw.githubusercontent.com/Rhilip/BangumiExtLinker/master/data/anime_map.json"
# Bangumi API 要求 UA 能认出是哪个应用，不带直接 403
UA = "dewe001/JaSubAuto (https://github.com/dewe001/JaSubAuto)"
CACHE_TTL = 6 * 3600      # 连载中的番剧集数会变，不能永久缓存
MAX_SEQUEL_HOPS = 3


@dataclass
class BangumiIds:
    """一集视频能拿到的 Bangumi ID，越具体越可信。"""

    episode: int | None = None   # 集 nfo：直接就是某条目的某一集
    season: int | None = None    # season.nfo 或 MoviePilot 的识别结果：这一季对应的条目
    show: int | None = None      # tvshow.nfo：剧级 ID，Jellyfin 插件里等于第一季的条目

    def __bool__(self) -> bool:
        return bool(self.episode or self.season or self.show)


def as_int(value) -> int | None:
    """Bangumi 的 ep/sort 是 JSON 数字，可能是 25.0；12.5 这种（总集篇）不算整数集号。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else None


# ---------- API ----------

_cache: dict[tuple, tuple[float, object]] = {}
_map_index: dict[int, dict] | None = None


def reset_cache() -> None:
    global _map_index
    _cache.clear()
    _map_index = None


def _api_get(path: str, params: dict | None = None):
    """所有 Bangumi API 调用的唯一出口，失败返回 None。单测把它整个换掉。"""
    try:
        return http.get_json(f"{API}{path}", headers={"User-Agent": UA}, params=params)
    except Exception:
        return None


def network_problem() -> str:
    """api.bgm.tv 最近连不上的原因（带括号），没问题返回空串。拼进说明里，一眼看出是网络问题。"""
    problem = http.host_problem(API)
    return f"（{problem}）" if problem else ""


def cached(key: tuple, fetch):
    """带过期的缓存。取到 None 不缓存，下次再试（多半是网络抖了一下）。"""
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    value = fetch()
    if value is not None:
        _cache[key] = (time.time(), value)
    return value


def subject(subject_id: int) -> dict | None:
    return cached(("subject", subject_id), lambda: _api_get(f"/v0/subjects/{subject_id}"))


def episode(episode_id: int) -> dict | None:
    return cached(("episode", episode_id), lambda: _api_get(f"/v0/episodes/{episode_id}"))


def titles(info: dict) -> list[str]:
    """条目的日文原名 + 别名。别名里常有和 AniList / Jimaku 一字不差的罗马音。"""
    raw = [info.get("name")]
    for item in info.get("infobox") or []:
        if item.get("key") != "别名":
            continue
        value = item.get("value")
        for v in value if isinstance(value, list) else [value]:
            raw.append(v.get("v") if isinstance(v, dict) else v)
    out: list[str] = []
    for t in raw:
        t = str(t or "").strip()
        if t and t not in out:
            out.append(t)
    return out


def main_episodes(subject_id: int) -> list[tuple[int | None, int]] | None:
    """正片（type=0）的 (sort, ep) 列表。取不到返回 None。"""
    def fetch():
        items: list[dict] = []
        while True:
            page = _api_get("/v0/episodes", {"subject_id": subject_id, "type": 0,
                                             "limit": 100, "offset": len(items)})
            if not isinstance(page, dict):
                return None
            data = page.get("data") or []
            items += data
            if not data or len(items) >= int(page.get("total") or 0):
                break
        return [(as_int(e.get("sort")), as_int(e.get("ep"))) for e in items
                if (as_int(e.get("ep")) or 0) >= 1]
    return cached(("episodes", subject_id), fetch)


def sequel(subject_id: int) -> int | None:
    """动画续集条目。有多个续集时不猜，返回 None。"""
    def fetch():
        rel = _api_get(f"/v0/subjects/{subject_id}/subjects")
        if not isinstance(rel, list):
            return None
        return [r.get("id") for r in rel if r.get("relation") == "续集" and r.get("type") == 2]
    ids = cached(("sequel", subject_id), fetch)
    return ids[0] if ids and len(ids) == 1 else None


# ---------- 集号定位 ----------

def locate(subject_id: int, number: int) -> tuple[int | None, int | None, str]:
    """媒体库里的第 `number` 集 → (条目, 条目内第几集, 说明)。定位不了返回 (None, None, 原因)。

    三种编号都见过：
      * 条目内编号：Season 2 目录里的 S02E01，就是这个条目的第 1 集
      * Bangumi 连续编号：咒术回战第二季条目里的第 25 集，其实是 ep=1
      * TMDB 连续编号跨季：芙莉莲 S01E36 超出第一季的 28 集，顺着「续集」走到第二季第 8 集
    跨季时"减去前面各季集数"和"找 sort"两种算法能同时算出来的，必须一致，不一致交人工。
    """
    sid, n = subject_id, number
    for hop in range(MAX_SEQUEL_HOPS + 1):
        eps = main_episodes(sid)
        if not eps:
            return None, None, f"取不到 Bangumi 条目 {sid} 的正片列表{network_problem()}"
        ep_numbers = {ep for _, ep in eps}
        sort_to_ep = {s: ep for s, ep in eps if s is not None}
        by_sort = sort_to_ep.get(number)

        if n in ep_numbers:
            if by_sort is not None and by_sort != n:
                return None, None, (f"第 {number} 集在 Bangumi 条目 {sid} 里既可能是第 {n} 集，"
                                    f"也可能是连续编号的第 {by_sort} 集，交人工")
            note = f"Bangumi 条目 {sid} 第 {n} 集"
            return sid, n, note + (f"（从第一季起顺着续集走了 {hop} 步）" if hop else "")
        if hop == 0 and by_sort is not None:
            return sid, by_sort, f"Bangumi 条目 {sid} 连续编号第 {number} 集 = 本季第 {by_sort} 集"
        if n < max(ep_numbers):
            return None, None, f"Bangumi 条目 {sid} 里没有第 {n} 集"

        nxt = sequel(sid)
        if nxt is None:
            return None, None, f"第 {n} 集超出 Bangumi 条目 {sid} 的 {max(ep_numbers)} 集，也找不到唯一的续集"
        sid, n = nxt, n - max(ep_numbers)
    return None, None, f"第 {number} 集顺着续集走了 {MAX_SEQUEL_HOPS} 步还没找到，交人工"


# ---------- 离线映射表 ----------

def ensure_map_file(force: bool = False) -> Path:
    path = settings.bangumi_map_file
    path.parent.mkdir(parents=True, exist_ok=True)
    stale = (force or not path.exists()
             or (time.time() - path.stat().st_mtime) > settings.anime_lists_refresh_days * 86400)
    if stale:
        path.write_bytes(http.get_bytes(MAP_URL, headers={"User-Agent": UA}, timeout=120))
    return path


def map_row(subject_id: int) -> dict | None:
    """BangumiExtLinker 里这个条目的那一行（含 mal_id / anidb_id），没有或读不到返回 None。

    这张表是自动匹配生成的，新番往往几个月都没有外链（芙莉莲第二季开播八个月后仍然没有），
    所以查不到是常态，调用方要有后手。
    """
    global _map_index
    if _map_index is None:
        try:
            rows = json.loads(ensure_map_file().read_text(encoding="utf-8"))
        except Exception as exc:
            trace.log(f"BangumiExtLinker 映射表读不到：{exc}")
            return None                       # 下载失败不缓存，下次再试
        index = {}
        for row in rows:
            sid = as_int(row.get("bgm_id"))
            if sid:
                index[sid] = row
        _map_index = index
    return _map_index.get(subject_id)
