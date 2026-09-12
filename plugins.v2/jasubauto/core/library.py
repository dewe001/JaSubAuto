"""浏览媒体库：让手动模式能按**中文剧名**选剧。

为什么需要这个：媒体库里的剧名是中文，而 Jimaku/AniList 只认罗马字或日文。
AniList 的标题搜索对中文名不可靠——实测「咒术回战」「间谍过家家」能搜到（AniList
的 synonyms 里恰好有中文），但「葬送的芙莉莲」搜到 0 条。所以中文名不能当检索入口。

真正可靠的中文名→ID 来源是媒体库自己：刮削时会在剧集根目录写 tvshow.nfo，
里面既有中文标题也有 ID。于是流程变成：
    浏览 /媒体 → 看到中文剧名 → 点一下 → 自动带出 ID 和文件夹路径 → 批量补扫
没有 nfo 的目录也照样列出来，只是 ID 需要人工填。

ID 有两种：MoviePilot 刮削写 tmdb；Jellyfin 的 Bangumi 插件刮削常常只有 Bangumi ID，
而且会细到每一季、每一集（见 `bangumi_ids_for`）。

交互上是**搜索**而不是**浏览**：番剧库动辄几百部，一进页面就铺满一屏没法用。
不输关键字只报个总数，输了才列匹配项——统计集数要递归扫目录，也只对匹配项做。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from . import bangumi, placer
from .settings import settings

# 单个目录里最多统计多少个视频文件，避免在超大目录上卡住
_SCAN_CAP = 500


@dataclass
class LibraryEntry:
    name: str
    path: str
    tmdb_id: int | None = None
    bangumi_id: int | None = None
    title: str = ""          # nfo 里的标题，通常就是中文名
    year: str = ""
    video_count: int = 0
    ja_count: int = 0        # 已有日语字幕的集数
    subdir_count: int = 0
    note: str = ""

    @property
    def missing(self) -> int:
        return max(self.video_count - self.ja_count, 0)


@dataclass
class ShowNfo:
    tmdb_id: int | None = None
    bangumi_id: int | None = None
    title: str = ""
    year: str = ""


# ---------- nfo ----------

def _nfo_root(path: Path):
    """解析一个 nfo，不存在或格式坏了返回 None。"""
    try:
        return ET.parse(path).getroot() if path.is_file() else None
    except Exception:
        return None


def _positive_int(el) -> int | None:
    text = (el.text or "").strip() if el is not None else ""
    return int(text) if text.isdigit() and int(text) > 0 else None


def _uniqueid(root, types: tuple[str, ...]) -> int | None:
    for el in root.findall("uniqueid"):
        if (el.get("type") or "").lower() in types and _positive_int(el):
            return _positive_int(el)
    return None


def tmdb_id_in(root) -> int | None:
    """MoviePilot 写 <uniqueid type="tmdb"> 和 <tmdbid>，Jellyfin 写 <tmdbid>。

    **不认 <id>**：Jellyfin 往 tvshow.nfo 的 <id> 里写的是 TVDB ID（SeriesNfoSaver），
    当成 tmdb 会拿别的剧去查映射表。
    """
    return (_uniqueid(root, ("tmdb", "themoviedb"))
            or _positive_int(root.find("tmdbid")) or _positive_int(root.find("tmdb_id")))


def bangumi_id_in(root) -> int | None:
    """Jellyfin 的 Bangumi 插件把 ID 存成 provider「Bangumi」，Jellyfin 写 nfo 时变成 <bangumiid>。
    别的工具可能写 <uniqueid type="bangumi">，两种都认。"""
    return _uniqueid(root, ("bangumi",)) or _positive_int(root.find("bangumiid"))


def read_nfo(folder: Path) -> ShowNfo:
    """从剧集目录的 nfo 里取 ID、标题、年份，取不到的留空。"""
    for name in ("tvshow.nfo", "movie.nfo"):
        root = _nfo_root(folder / name)
        if root is None:
            continue
        return ShowNfo(
            tmdb_id=tmdb_id_in(root),
            bangumi_id=bangumi_id_in(root),
            title=(root.findtext("title") or root.findtext("originaltitle") or "").strip(),
            year=(root.findtext("year") or "").strip(),
        )
    return ShowNfo()


def bangumi_ids_for(video: Path, show: int | None = None,
                    season: int | None = None) -> bangumi.BangumiIds:
    """一集视频能找到的 Bangumi ID。显式传入的优先，缺的从 nfo 补。

    Jellyfin 的 Bangumi 插件（媒体库开了 nfo 保存时）会写三层：
        <剧>/tvshow.nfo                 剧级 ID，等于第一季的条目
        <剧>/Season 2/season.nfo        这一季的条目
        <剧>/Season 2/<视频名>.nfo       这一集在 Bangumi 上的集 ID
    """
    ids = bangumi.BangumiIds(show=show, season=season)
    root = _nfo_root(video.with_suffix(".nfo"))
    if root is not None:
        ids.episode = bangumi_id_in(root)
    if ids.season is None:
        root = _nfo_root(video.parent / "season.nfo")
        if root is not None:
            ids.season = bangumi_id_in(root)
    if ids.show is None:
        for folder in list(video.parents)[:3]:
            root = _nfo_root(folder / "tvshow.nfo")
            if root is not None:
                ids.show = bangumi_id_in(root)
                break
    return ids


# ---------- 搜剧 ----------

def _count(folder: Path) -> tuple[int, int]:
    """(视频数, 已有日语字幕的视频数)。"""
    videos = 0
    with_ja = 0
    for p in folder.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in placer.VIDEO_EXTS:
            continue
        videos += 1
        if placer.existing_ja_subtitle(p):
            with_ja += 1
        if videos >= _SCAN_CAP:
            break
    return videos, with_ja


# 一次最多为多少个目录做递归统计：统计要 rglob 整个剧集目录，库大了很慢
_STATS_BUDGET = 30
# 搜索结果上限，防止关键字太宽（比如只输一个"の"）时列出几百项
_RESULT_LIMIT = 50


def _show_dirs(root: Path) -> list[Path]:
    """番剧库的标准布局是 <库根>/<剧名>/Season N/<文件>，所以剧集目录就是根的直接子目录。"""
    try:
        return [c for c in sorted(root.iterdir(), key=lambda p: p.name.lower())
                if c.is_dir() and not c.name.startswith(".")]
    except (OSError, PermissionError):
        return []


def _entry_for(child: Path, nfo: ShowNfo, with_stats: bool) -> LibraryEntry:
    e = LibraryEntry(name=child.name, path=str(child), tmdb_id=nfo.tmdb_id,
                     bangumi_id=nfo.bangumi_id, title=nfo.title, year=nfo.year)
    try:
        e.subdir_count = sum(1 for _ in child.iterdir() if _.is_dir())
        if with_stats:
            e.video_count, e.ja_count = _count(child)
    except (OSError, PermissionError):
        e.note = "无权限读取"
    if nfo.tmdb_id is None and nfo.bangumi_id is None:
        e.note = e.note or "nfo 里没有 TMDB / Bangumi ID，需要手动填"
    return e


def _matches(q: str, child: Path, nfo: ShowNfo) -> bool:
    return q in child.name.lower() or q in (nfo.title or "").lower()


def find_shows(q: str = "", root: str = "") -> dict:
    """在配置的番剧库里按关键字找剧。

    `q` 为空时**不返回任何剧**，只报每个库有多少部——这是刻意的：
    一进页面就把整库铺出来既慢又没法看。
    """
    roots = [root] if root else settings.media_root_list
    q = (q or "").strip().lower()

    root_info, missing = [], []
    all_shows: list[tuple[Path, Path]] = []          # (库根, 剧集目录)
    for r in roots:
        rp = placer.map_path(r)
        if not rp.is_dir():
            missing.append(r)
            root_info.append({"path": r, "count": 0, "ok": False})
            continue
        shows = _show_dirs(rp)
        root_info.append({"path": r, "count": len(shows), "ok": True})
        all_shows += [(rp, c) for c in shows]

    if not roots:
        return {"roots": [], "entries": [], "total": 0,
                "note": "还没配置番剧库目录。请在插件配置里填「番剧库目录」，"
                        "例如 /媒体/日番（注意不要填 /媒体，那会把下载目录也扫进来）"}
    if missing:
        return {"roots": root_info, "entries": [], "total": len(all_shows),
                "note": "这些目录看不到：" + "、".join(missing)}
    if not q:
        return {"roots": root_info, "entries": [], "total": len(all_shows),
                "note": f"番剧库里共 {len(all_shows)} 部，输入剧名关键字查找（中文名即可）"}

    matched = []
    for _rp, child in all_shows:
        nfo = read_nfo(child)
        if _matches(q, child, nfo):
            matched.append((child, nfo))

    truncated = len(matched) > _RESULT_LIMIT
    shown = matched[:_RESULT_LIMIT]
    with_stats = len(shown) <= _STATS_BUDGET
    entries = [_entry_for(c, nfo, with_stats) for c, nfo in shown]

    if not entries:
        note = f"「{q}」没匹配到（库里共 {len(all_shows)} 部）"
    elif truncated:
        note = f"「{q}」匹配到 {len(matched)} 部，只显示前 {_RESULT_LIMIT} 部，关键字再具体些"
    elif not with_stats:
        note = f"「{q}」匹配到 {len(entries)} 部（超过 {_STATS_BUDGET} 部就不统计集数了）"
    else:
        note = f"「{q}」匹配到 {len(entries)} 部"

    return {"roots": root_info, "total": len(all_shows),
            "entries": [e.__dict__ | {"missing": e.missing} for e in entries], "note": note}


def browse(path: str, q: str = "", deep_stats: bool | None = None) -> dict:
    """列出指定目录下的子目录。给「库里找不到、想手工指定路径」的情况兜底。"""
    folder = placer.map_path(path) if path else None
    if not folder or not folder.is_dir():
        return {"path": path, "parent": None, "entries": [], "total": 0,
                "note": f"不是有效目录（可能看不到这个路径）：{folder}"}

    q = (q or "").strip().lower()
    children = _show_dirs(folder)
    prepared = []
    for child in children:
        nfo = read_nfo(child)
        if q and not _matches(q, child, nfo):
            continue
        prepared.append((child, nfo))

    with_stats = deep_stats if deep_stats is not None else len(prepared) <= _STATS_BUDGET
    entries = [_entry_for(c, nfo, with_stats) for c, nfo in prepared]
    note = ""
    if not entries:
        note = f"「{q}」没匹配到（共 {len(children)} 项）" if q else "这个目录下没有子文件夹"
    elif not with_stats:
        note = f"共 {len(children)} 项，输入关键字筛选后才统计集数"

    return {"path": str(folder),
            "parent": str(folder.parent) if folder.parent != folder else None,
            "total": len(children),
            "entries": [e.__dict__ | {"missing": e.missing} for e in entries], "note": note}
