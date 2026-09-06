"""番剧识别：把 MoviePilot 侧的 (tmdb_id, 季, 集) 换算成 Jimaku 侧的 (anilist_id, 集)。

为什么需要换算：TMDB 和 AniList 的分季/编号体系不一致。例如《葬送のフリーレン》：
  - TMDB：一个剧集，第 2 季的剧集继续排在 S01E29 之后
  - AniList：S1(154587) 和 S2(182255) 是两个独立条目，各自从第 1 集数起
映射表 Fribb/anime-lists 自带 episode_offset 字段，正好用来做这个换算。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import http
from .settings import settings

MAPPING_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
ANILIST_API = "https://graphql.anilist.co"
UA = "JaSubAuto/0.5.1 (+https://github.com/dewe001/JaSubAuto)"


@dataclass
class Resolution:
    """识别结果。anilist_id 为 None 表示没认出来。"""

    anilist_id: int | None = None
    episode: int | None = None          # 换算到 AniList 体系后的集号
    source: str = ""                    # mapping / anilist-search / manual
    confident: bool = False             # False 时不应自动下载，交给人工确认
    note: str = ""                      # 人类可读的解释，日志和网页都用它
    candidates: list[dict] = field(default_factory=list)  # 歧义时的候选行


# ---------- 映射表 ----------

def ensure_mapping_file(force: bool = False) -> Path:
    """确保本地有映射表，过期则重新下载。"""
    path = settings.anime_lists_file
    path.parent.mkdir(parents=True, exist_ok=True)
    stale = (
        force
        or not path.exists()
        or (time.time() - path.stat().st_mtime) > settings.anime_lists_refresh_days * 86400
    )
    if stale:
        path.write_bytes(http.get_bytes(MAPPING_URL, headers={"User-Agent": UA}, timeout=120))
    return path


_index: dict[tuple[int, int | None], list[dict]] | None = None


def _load_index() -> dict[tuple[int, int | None], list[dict]]:
    """建 (tmdb_tv_id, tmdb_season) -> [映射行] 的索引。"""
    global _index
    if _index is not None:
        return _index
    rows = json.loads(ensure_mapping_file().read_text(encoding="utf-8"))
    idx: dict[tuple[int, int | None], list[dict]] = {}
    for row in rows:
        tmdb = row.get("themoviedb_id")
        if not isinstance(tmdb, dict) or not row.get("anilist_id"):
            continue
        tv_id = tmdb.get("tv")
        if not tv_id:
            continue
        season = row.get("season")
        season_no = season.get("tmdb") if isinstance(season, dict) else None
        if isinstance(season_no, str):
            season_no = int(season_no) if season_no.isdigit() else None
        idx.setdefault((tv_id, season_no), []).append(row)
    _index = idx
    return idx


def _offset(row: dict) -> int:
    off = row.get("episode_offset")
    return off.get("tmdb", 0) if isinstance(off, dict) else 0


def resolve_by_mapping(tmdb_id: int, season: int, episode: int) -> Resolution:
    """用离线映射表识别。这是主路径——绝不用标题字符串做主匹配。"""
    rows = _load_index().get((tmdb_id, season), [])
    if not rows:
        return Resolution(note=f"映射表里没有 tmdb={tmdb_id} season={season} 的条目")

    if season == 0:
        # 特典/OVA/剧场版：TMDB 的 S0 是个大杂烩，映射歧义几乎都出在这里，不自动猜
        return Resolution(
            note="季号为 0（特典/OVA），编号体系不可靠，交给人工确认",
            candidates=rows,
        )

    # 按 offset 从大到小试：找第一个能让"集号减去偏移"仍 >= 1 的条目
    for row in sorted(rows, key=_offset, reverse=True):
        converted = episode - _offset(row)
        if converted >= 1:
            note = f"映射表命中 anilist={row['anilist_id']}"
            if _offset(row):
                note += f"，集号偏移 {_offset(row)}：S{season}E{episode} → 第 {converted} 集"
            return Resolution(
                anilist_id=row["anilist_id"],
                episode=converted,
                source="mapping",
                confident=True,
                note=note,
                candidates=rows,
            )

    return Resolution(note=f"映射表有 {len(rows)} 条候选但集号都对不上", candidates=rows)


# ---------- AniList 标题搜索兜底 ----------

_ANILIST_QUERY = """
query ($search: String) {
  Page(page: 1, perPage: 10) {
    media(search: $search, type: ANIME) {
      id episodes format seasonYear
      title { romaji english native }
      synonyms
    }
  }
}
"""


def search_anilist(title: str) -> list[dict]:
    """按标题搜 AniList。只在映射表查不到时用，且结果不算 confident。

    注意：传进来的应该是 original_title / en_title（罗马字或英文），
    不要传媒体库里的中文名。
    """
    # AniList 的 Cloudflare 会拦掉默认 UA，必须显式带一个
    data = http.post_json(ANILIST_API,
                          {"query": _ANILIST_QUERY, "variables": {"search": title}},
                          headers={"User-Agent": UA, "Content-Type": "application/json"})
    return data.get("data", {}).get("Page", {}).get("media", []) or []


def resolve(tmdb_id: int | None, season: int, episode: int, title: str = "") -> Resolution:
    """完整识别流程：映射表优先，查不到才用标题兜底。"""
    if tmdb_id:
        result = resolve_by_mapping(tmdb_id, season, episode)
        if result.anilist_id:
            return result
        fallback_note = result.note
        fallback_candidates = result.candidates
    else:
        fallback_note = "未提供 tmdb_id"
        fallback_candidates = []

    if not title:
        return Resolution(note=f"{fallback_note}；没有标题可供兜底搜索", candidates=fallback_candidates)

    hits = search_anilist(title)
    if not hits:
        return Resolution(note=f"{fallback_note}；AniList 也搜不到「{title}」", candidates=fallback_candidates)

    top = hits[0]
    return Resolution(
        anilist_id=top["id"],
        episode=episode,  # 兜底路径没有偏移信息，只能原样用，风险自负
        source="anilist-search",
        confident=False,  # 标题搜索永远不算可信，必须人工确认
        note=(
            f"{fallback_note}；改用标题搜索命中 anilist={top['id']} "
            f"({top['title'].get('romaji')})，未经偏移校正，需人工确认"
        ),
        candidates=hits,
    )
