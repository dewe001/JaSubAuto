"""番剧识别：把 MoviePilot 侧的 (tmdb_id, 季, 集) 换算成 Jimaku 侧的 (anilist_id, 集)。

为什么需要换算：TMDB 和 AniList 的分季/编号体系不一致。例如《葬送のフリーレン》：
  - TMDB：一个剧集，第 2 季的剧集继续排在 S01E29 之后
  - AniList：S1(154587) 和 S2(182255) 是两个独立条目，各自从第 1 集数起
映射表 Fribb/anime-lists 自带 episode_offset 字段，正好用来做这个换算。

只有 Bangumi ID 的剧（Jellyfin 的 Bangumi 插件刮削）走 `resolve_bangumi`，细节见 bangumi.py。
"""

from __future__ import annotations

import json
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import bangumi, http, jimaku
from .settings import settings

MAPPING_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
ANILIST_API = "https://graphql.anilist.co"
UA = "JaSubAuto (+https://github.com/dewe001/JaSubAuto)"
# Bangumi 条目按标题找 Jimaku 条目时最多搜几次（Jimaku 限速 25 次/分钟）
MAX_TITLE_QUERIES = 3


@dataclass
class Resolution:
    """识别结果。anilist_id 为 None 表示没认出来。"""

    anilist_id: int | None = None
    episode: int | None = None          # 换算到 AniList 体系后的集号
    source: str = ""                    # mapping / bangumi / anilist-search / manual
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
_id_index: dict[str, dict[int, set[int]]] | None = None


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


def _load_id_index() -> dict[str, dict[int, set[int]]]:
    """MAL / AniDB ID → AniList ID。Bangumi 那张映射表只给到 MAL/AniDB，靠这张表接上。"""
    global _id_index
    if _id_index is not None:
        return _id_index
    rows = json.loads(ensure_mapping_file().read_text(encoding="utf-8"))
    idx: dict[str, dict[int, set[int]]] = {"mal_id": {}, "anidb_id": {}}
    for row in rows:
        anilist_id = row.get("anilist_id")
        if not anilist_id:
            continue
        for key, table in idx.items():
            value = row.get(key)
            if isinstance(value, int):
                table.setdefault(value, set()).add(anilist_id)
    _id_index = idx
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


# ---------- Bangumi ----------

def _norm(text) -> str:
    """标题全等比对前的归一化：全半角、大小写、弯引号、多余空白。不做任何模糊处理。"""
    s = unicodedata.normalize("NFKC", str(text or "")).casefold()
    s = s.replace("’", "'").replace("‘", "'").replace("`", "'")
    return " ".join(s.split())


_ANILIST_BY_ID = "query ($id: Int) { Media(id: $id, type: ANIME) { startDate { year } } }"


def anilist_start_year(anilist_id: int) -> int | None:
    """AniList 条目的开播年份，查不到返回 None。用来核对同名条目是不是同一部（重制版同名）。"""
    try:
        data = http.post_json(ANILIST_API,
                              {"query": _ANILIST_BY_ID, "variables": {"id": anilist_id}},
                              headers={"User-Agent": UA, "Content-Type": "application/json"})
        media = (data.get("data") or {}).get("Media") or {}
        return (media.get("startDate") or {}).get("year")
    except Exception:
        return None


def _bangumi_to_anilist(subject_id: int) -> tuple[int | None, str, bool]:
    """Bangumi 条目 → AniList 条目，返回 (anilist_id, 说明, 结论是否确定)。

    结论不确定（网络失败）的不缓存，确定的（找到 / 确认找不到 / 确认有歧义）缓存。
    """
    row = bangumi.map_row(subject_id)
    if row:
        idx = _load_id_index()
        found: dict[int, str] = {}
        for key, label in (("mal_id", "MAL"), ("anidb_id", "AniDB")):
            value = bangumi.as_int(row.get(key))
            for anilist_id in idx[key].get(value, ()) if value else ():
                found.setdefault(anilist_id, label)
        if len(found) == 1:
            anilist_id, label = next(iter(found.items()))
            return anilist_id, f"经 {label} 映射到 anilist={anilist_id}", True
        if len(found) > 1:
            return None, f"Bangumi 条目 {subject_id} 映射出多个 AniList 条目 {sorted(found)}，交人工", True

    # 映射表没有外链：用条目的日文原名和别名，找 Jimaku 上**一字不差**的条目
    info = bangumi.subject(subject_id)
    if not info:
        return None, f"取不到 Bangumi 条目 {subject_id}", False
    names = bangumi.titles(info)
    if not names:
        return None, f"Bangumi 条目 {subject_id} 没有标题", True
    wanted = {_norm(t) for t in names} - {""}
    # Jimaku 的条目名就是 AniList 的罗马音，所以先用罗马音/英文别名搜，再用日文名
    queries = sorted(names, key=lambda t: not t.isascii())[:MAX_TITLE_QUERIES]
    hits: dict[int, str] = {}
    for query in queries:
        try:
            entries = jimaku.search_by_title(query)
        except Exception as exc:
            return None, f"按标题查 Jimaku 失败：{exc}", False
        for e in entries:
            if not e.get("anilist_id") or not (e.get("flags") or {}).get("anime", True):
                continue
            if {_norm(e.get(k)) for k in ("name", "japanese_name", "english_name")} & wanted:
                hits[e["anilist_id"]] = e.get("name") or ""
        if hits:
            break

    title = names[0]
    if not hits:
        return None, f"「{title}」映射表里没有外链，Jimaku 上也没有同名条目", True
    if len(hits) > 1:
        return None, f"「{title}」在 Jimaku 上有多个同名条目 {sorted(hits)}，交人工", True
    anilist_id, jimaku_name = next(iter(hits.items()))
    year = anilist_start_year(anilist_id)
    bgm_year = (info.get("date") or "")[:4]
    if year is None:
        return None, f"「{title}」同名命中 anilist={anilist_id}，但查不到开播年份无法核对，交人工", False
    if not bgm_year.isdigit() or abs(int(bgm_year) - year) > 1:
        return None, (f"「{title}」同名命中 anilist={anilist_id}，但开播年份对不上"
                      f"（Bangumi {bgm_year or '未知'} / AniList {year}），交人工"), True
    return anilist_id, f"与 Jimaku 条目「{jimaku_name}」同名、开播年份一致 → anilist={anilist_id}", True


def bangumi_to_anilist(subject_id: int) -> tuple[int | None, str]:
    key = ("anilist", subject_id)
    hit = bangumi.cached(key, lambda: None)
    if hit is not None:
        return hit
    anilist_id, note, settled = _bangumi_to_anilist(subject_id)
    if settled:
        bangumi.cached(key, lambda: (anilist_id, note))
    return anilist_id, note


def resolve_bangumi(ids: bangumi.BangumiIds, season: int | None, episode: int) -> Resolution:
    """用 Bangumi ID 识别。任何一步拿不准都返回 anilist_id=None，由调用方交人工。

    ID 越具体越优先：集 nfo（直接是某条目的某一集）> season.nfo > tvshow.nfo。
    剧级 ID 在 Jellyfin 插件里等于第一季的条目，所以**只能用于第 1 季**；
    第 2 季往后没有 season.nfo 就不猜——TMDB 的第 2 季未必是 Bangumi 的下一个条目（分割放送）。
    """
    try:
        if season == 0:
            return Resolution(note="季号为 0（特典/OVA），编号体系不可靠，交给人工确认")

        subject_id = ep = None
        how = ""
        if ids.episode:
            info = bangumi.episode(ids.episode) or {}
            ep_no, sort_no = bangumi.as_int(info.get("ep")), bangumi.as_int(info.get("sort"))
            # 集号对不上说明 nfo 过期或刮削错了集，不用它，往下走条目级 ID
            if info.get("type") == 0 and info.get("subject_id") and ep_no and episode in (ep_no, sort_no):
                subject_id, ep = int(info["subject_id"]), ep_no
                how = f"集 nfo 指向 Bangumi 条目 {subject_id} 第 {ep} 集"

        if subject_id is None:
            start = ids.season or (ids.show if (season or 1) == 1 else None)
            if start is None:
                return Resolution(note=f"第 {season} 季目录里没有 season.nfo 的 Bangumi ID，"
                                       f"剧级 ID 只对应第一季，交人工")
            subject_id, ep, how = bangumi.locate(start, episode)
            if subject_id is None:
                return Resolution(note=how)

        anilist_id, map_note = bangumi_to_anilist(subject_id)
        if not anilist_id:
            return Resolution(note=f"{how}；{map_note}")
        return Resolution(anilist_id=anilist_id, episode=ep, source="bangumi", confident=True,
                          note=f"{how}，{map_note}")
    except Exception as exc:                          # 失败必须静默降级
        return Resolution(note=f"按 Bangumi ID 识别出错：{exc}")


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


def resolve(tmdb_id: int | None, season: int, episode: int, title: str = "",
            bangumi_ids: bangumi.BangumiIds | None = None) -> Resolution:
    """完整识别流程：TMDB 映射表 → Bangumi ID → 标题兜底（不可信）。"""
    notes: list[str] = []
    fallback_candidates: list[dict] = []
    if tmdb_id:
        result = resolve_by_mapping(tmdb_id, season, episode)
        if result.anilist_id:
            return result
        notes.append(result.note)
        fallback_candidates = result.candidates
    else:
        notes.append("未提供 tmdb_id")

    if bangumi_ids:
        result = resolve_bangumi(bangumi_ids, season, episode)
        if result.anilist_id:
            return result
        notes.append(result.note)

    fallback_note = "；".join(notes)
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
