"""批量补扫：给一个番剧文件夹，为里面缺日语字幕的每一集补上。

典型用法（手动模式的主力场景）：
    输入 /媒体/动漫/番剧A + 该剧的 tmdb_id（或 Bangumi ID；都不填就读 tvshow.nfo）
    → 递归找出所有视频文件，逐个解析季/集号
    → 已经有 .ja.* 字幕的直接跳过
    → 其余的走「识别 → 查 Jimaku → 选文件 → 落盘」，字幕名与视频同名

设计要点：
  * 一次扫描只对每个 AniList 条目查一次 Jimaku 文件列表（限速 25 请求/分钟，不能逐集查）
  * 判「已有字幕」看的是任意 .ja.* 扩展名，不只是这次准备写的那个，
    否则 X.ja.ass 已存在时还会再写一个 X.ja.srt
  * 每一集单独记状态，任何一集失败都不影响其余（失败必须静默降级）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import bangumi, identify, jimaku, library, picker, placer, trace
from .settings import settings

# 从视频文件名里直接取季+集，媒体库经 MoviePilot 整理后基本都是这个格式
_SE = re.compile(r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,3})")
# 季文件夹：Season 2 / S2 / 第2季 / 第二季
_SEASON_DIR = re.compile(r"(?:season|s)\s*(\d{1,2})|第\s*(\d{1,2})\s*季", re.IGNORECASE)
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


@dataclass
class EpisodeResult:
    video: str
    season: int | None = None
    library_episode: int | None = None
    anilist_id: int | None = None
    anilist_episode: int | None = None
    status: str = ""          # skipped_existing / ok / dry_run / not_found / needs_review / unparsed / unidentified / error
    picked: str = ""
    lang: str = ""
    target: str = ""
    reason: str = ""
    replaced: str = ""        # 覆盖掉的旧字幕文件名
    log: list[str] = field(default_factory=list)   # 这一集的识别过程，页面上可展开


@dataclass
class ScanReport:
    root: str
    dry_run: bool
    total_videos: int = 0
    episodes: list[EpisodeResult] = field(default_factory=list)
    note: str = ""
    log: list[str] = field(default_factory=list)

    @property
    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for e in self.episodes:
            counts[e.status] = counts.get(e.status, 0) + 1
        return counts


def season_from_dir(path: Path) -> int | None:
    """从所在目录名推季号，推不出返回 None。"""
    for part in reversed(path.parts[:-1]):
        m = _SEASON_DIR.search(part)
        if m:
            return int(m.group(1)) if m.group(1) else int(m.group(2))
        for cn, num in _CN_NUM.items():
            if f"第{cn}季" in part:
                return num
    return None


def parse_video(path: Path) -> tuple[int | None, int | None]:
    """从视频路径解析 (季号, 集号)，媒体库口径。解析不出的位置返回 None。"""
    m = _SE.search(path.name)
    if m:
        return int(m.group(1)), int(m.group(2))
    ep = picker.parse_episode(path.name)
    if ep is None:
        return None, None
    # 文件名里没写季号，就看目录；目录也看不出按第 1 季算
    return season_from_dir(path) or 1, ep


def find_videos(root: Path) -> list[Path]:
    """递归找出所有视频文件，按路径排序。"""
    return sorted(
        (p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in placer.VIDEO_EXTS),
        key=lambda p: str(p).lower(),
    )


def _process_one(tmdb_id, title: str, season, episode, video_path: str,
                 overwrite: bool = False, source_name: str = "", bangumi_id=None) -> dict:
    """处理单个视频文件：识别 → 查 Jimaku → 选文件 → 落盘。

    自动模式（入库事件）和单集手动下载共用这一份，避免两条路径behavior 不一致。
    认不准是哪部剧、哪一集就返回 needs_review 而不是硬选（见 CLAUDE.md 硬性约束）；
    同一集的多个版本由 picker 按规则挑。`source_name` 是下载时的原始文件名——
    入库重命名后发布组只在它里面还看得到，picker 用它优先挑同组字幕。
    `bangumi_id` 是 MoviePilot 用 Bangumi 识别时给出的条目，对应这一季；
    没有 tmdb 时还会读视频旁边 nfo 里的 Bangumi ID。
    """
    out = {"video": video_path, "season": season, "episode": episode}
    if episode is None:
        return {**out, "status": "unparsed", "reason": "文件名里解析不出集号（可能是特典/剧场版），跳过"}

    # 已有日语字幕就到此为止：放在识别之前，省掉一次 Jimaku 查询和一次下载。
    # 勾了覆盖（只可能来自手动模式）就不跳过，照常走完流程再覆盖写入。
    video, _note = placer.resolve_video(video_path, episode)
    if video is not None and not overwrite:
        existing = placer.existing_ja_subtitle(video)
        if existing:
            return {**out, "status": "skipped_existing", "reason": f"已有日语字幕 {existing.name}，跳过"}

    # 读几个小 nfo 文件不费事；Bangumi ID 只在 tmdb 查不到时才会用上
    ids = (library.bangumi_ids_for(video, season=bangumi_id) if video is not None
           else bangumi.BangumiIds(season=bangumi_id))
    result = identify.resolve(tmdb_id, season if season is not None else 1, episode, title,
                              bangumi_ids=ids)
    if not result.anilist_id:
        return {**out, "status": "unidentified", "reason": result.note}
    out["anilist_id"] = result.anilist_id
    out["anilist_episode"] = result.episode
    if not result.confident:
        return {**out, "status": "needs_review", "reason": result.note}

    entries = jimaku.search_entries(result.anilist_id)
    if not entries:
        return {**out, "status": "not_found",
                "reason": f"Jimaku 无 anilist_id={result.anilist_id} 的条目"}

    files = jimaku.list_files(entries[0]["id"])
    cands, chosen, reason = picker.pick(files, result.episode,
                                        hint=f"{source_name} {Path(video_path).name}")
    trace.log(f"候选 {len(cands)} 个：{reason}")
    if not chosen:
        return {**out, "status": "needs_review" if cands else "not_found",
                "reason": reason, "candidate_count": len(cands)}

    content = None if settings.dry_run else jimaku.download(chosen.url)
    # library_episode 传媒体库口径的集号（视频文件名里那个），不是换算到 AniList 之后的
    outcome = placer.place(video_path, chosen.name, content, library_episode=episode,
                           overwrite=overwrite)
    return {**out,
            "status": "ok" if outcome.written else "dry_run",
            "picked": chosen.name, "lang": chosen.lang_label,
            "target": str(outcome.target), "reason": outcome.reason}


def process_one(tmdb_id, title: str, season, episode, video_path: str,
                overwrite: bool = False, source_name: str = "", bangumi_id=None) -> dict:
    """处理单个视频文件（见 `_process_one`），结果里带上这一集的识别过程 `log`。"""
    with trace.capture() as lines:
        out = _process_one(tmdb_id, title, season, episode, video_path,
                           overwrite=overwrite, source_name=source_name, bangumi_id=bangumi_id)
    return {**out, "log": lines}


def scan(
    root_path: str,
    tmdb_id: int | None = None,
    anilist_id: int | None = None,
    dry_run: bool | None = None,
    max_episodes: int = 200,
    overwrite: bool = False,
    bangumi_id: int | None = None,
) -> ScanReport:
    """扫描一个番剧文件夹并补齐日语字幕。

    `overwrite=True` 时已有日语字幕的集数照样重下并覆盖（手动模式专用，
    用来把之前下的纯日语换成中日双语）。

    tmdb_id / bangumi_id / anilist_id 至少一个，都不给就读文件夹里的 tvshow.nfo：
      * 给 tmdb_id：逐集走映射表换算，能正确处理分季错位（推荐）
      * 给 bangumi_id：nfo 里只有 Bangumi ID 的剧。各季 season.nfo、各集 nfo 里
        有更具体的 ID 会自动用上；tmdb_id 查不到时也会接着试 nfo 里的 Bangumi ID
      * 给 anilist_id：跳过识别，直接把文件名里的集号当 AniList 集号用，
        适合映射表查不到、用户已在网页上人工确认了条目的情况
    """
    dry_run = settings.dry_run if dry_run is None else dry_run
    root = placer.map_path(root_path) if root_path else Path("")
    report = ScanReport(root=str(root), dry_run=dry_run)

    if not root_path or not root_path.strip():
        report.note = "未提供文件夹路径"
        return report
    if not root.exists():
        report.note = f"文件夹不存在（路径映射可能不对）：{root}"
        return report
    if not root.is_dir():
        report.note = f"这不是文件夹：{root}"
        return report
    if tmdb_id is None and anilist_id is None and bangumi_id is None:
        nfo = library.read_nfo(root)
        tmdb_id, bangumi_id = nfo.tmdb_id, nfo.bangumi_id
        report.log.append(f"没填 ID，从 tvshow.nfo 读到 tmdb={nfo.tmdb_id} bangumi={nfo.bangumi_id}")
        if tmdb_id is None and bangumi_id is None:
            report.note = ("没有 TMDB / Bangumi / AniList ID，文件夹里的 tvshow.nfo 也没有，"
                           "无法确定这个文件夹是哪部剧")
            return report

    videos = find_videos(root)
    report.total_videos = len(videos)
    if not videos:
        report.note = f"文件夹里没有视频文件：{root}"
        return report
    if len(videos) > max_episodes:
        report.note = f"视频文件超过 {max_episodes} 个（{len(videos)}），为安全起见只处理前 {max_episodes} 个"
        videos = videos[:max_episodes]

    # ---- 第一遍：解析集号、剔掉已有字幕的，确定每一集对应哪个 AniList 条目 ----
    todo: list[EpisodeResult] = []
    for video in videos:
        season, ep = parse_video(video)
        r = EpisodeResult(video=str(video), season=season, library_episode=ep)

        existing = placer.existing_ja_subtitle(video)
        if existing and not overwrite:
            r.status, r.reason = "skipped_existing", f"已有日语字幕：{existing.name}"
            report.episodes.append(r)
            continue
        if ep is None:
            r.status, r.reason = "unparsed", "文件名里解析不出集号（可能是特典/剧场版），跳过"
            report.episodes.append(r)
            continue

        if anilist_id is not None:
            r.anilist_id, r.anilist_episode = anilist_id, ep
        else:
            with trace.capture() as lines:
                ids = library.bangumi_ids_for(video, show=bangumi_id)
                res = identify.resolve(tmdb_id, season if season is not None else 1, ep, "",
                                       bangumi_ids=ids)
            r.log += lines
            if not res.anilist_id:
                r.status, r.reason = "unidentified", res.note
                report.episodes.append(r)
                continue
            if not res.confident:
                r.status, r.reason = "needs_review", res.note
                r.anilist_id, r.anilist_episode = res.anilist_id, res.episode
                report.episodes.append(r)
                continue
            r.anilist_id, r.anilist_episode = res.anilist_id, res.episode

        todo.append(r)
        report.episodes.append(r)

    # ---- 第二遍：每个 AniList 条目只查一次 Jimaku 文件列表（限速 25/分钟）----
    files_cache: dict[int, list[dict]] = {}
    for r in todo:
        if r.anilist_id in files_cache:
            continue
        with trace.capture() as lines:
            try:
                entries = jimaku.search_entries(r.anilist_id)
                files_cache[r.anilist_id] = jimaku.list_files(entries[0]["id"]) if entries else []
                trace.log(f"Jimaku：anilist={r.anilist_id} 有 {len(entries)} 个条目、"
                          f"{len(files_cache[r.anilist_id])} 个字幕文件")
            except Exception as exc:                      # 失败必须静默降级
                files_cache[r.anilist_id] = []
                r.reason = f"查 Jimaku 失败：{exc}"
        r.log += lines

    # ---- 第三遍：逐集选文件并落盘 ----
    for r in todo:
        files = files_cache.get(r.anilist_id) or []
        if not files:
            r.status = "not_found"
            r.reason = r.reason or f"Jimaku 上没有 anilist_id={r.anilist_id} 的字幕文件"
            continue
        # 媒体库里的文件名一般已被重命名，但命名模板里带了发布组时也能用上
        cands, chosen, reason = picker.pick(files, r.anilist_episode, hint=Path(r.video).name)
        r.log.append(f"候选 {len(cands)} 个：{reason}")
        if not chosen:
            r.status = "not_found" if not cands else "needs_review"
            r.reason = reason
            continue
        r.picked, r.lang = chosen.name, chosen.lang_label
        try:
            content = None if dry_run else jimaku.download(chosen.url)
            outcome = placer.place(str(Path(r.video)), chosen.name, content, dry_run=dry_run,
                                   overwrite=overwrite)
            r.status = "ok" if outcome.written else ("dry_run" if dry_run else "error")
            r.target, r.reason, r.replaced = str(outcome.target), outcome.reason, outcome.replaced
        except Exception as exc:
            r.status, r.reason = "error", f"下载/落盘失败：{exc}"

    return report


if __name__ == "__main__":                            # 命令行自测
    import argparse, json, sys
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="批量补扫一个番剧文件夹")
    ap.add_argument("--dir", required=True)
    ap.add_argument("--tmdb-id", type=int)
    ap.add_argument("--bangumi-id", type=int)
    ap.add_argument("--anilist-id", type=int)
    ap.add_argument("--write", action="store_true", help="真正写盘（默认 dry-run）")
    ap.add_argument("--overwrite", action="store_true", help="已有日语字幕也重下覆盖")
    a = ap.parse_args()
    rep = scan(a.dir, a.tmdb_id, a.anilist_id, dry_run=not a.write, overwrite=a.overwrite,
               bangumi_id=a.bangumi_id)
    print(f"{rep.root}  视频 {rep.total_videos} 个  dry_run={rep.dry_run}")
    if rep.note:
        print("  ! " + rep.note)
    for e in rep.episodes:
        print(f"  [{e.status:<16}] E{e.library_episode or '?':<3} {Path(e.video).name}")
        if e.picked:
            print(f"       ← {e.picked}  ({e.lang})")
        if e.reason:
            print(f"       {e.reason}")
    print("  汇总：" + json.dumps(rep.summary, ensure_ascii=False))
