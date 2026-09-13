"""脱机调试用的 FastAPI 壳子：把 core/ 的能力按和插件一致的路径暴露出来。

生产环境不需要它——装插件就够了。它的用途是不启动 MoviePilot 也能调核心逻辑，
接口路径与插件 API 一一对应（插件是 /api/v1/plugin/JaSubAuto/xxx，这里是 /api/xxx），
共用同一份 ui.html，所以两边界面完全一样。

    uvicorn service.main:app --port 8990
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Body, FastAPI, Query
from fastapi.responses import HTMLResponse

from . import PLUGIN_DIR  # noqa: F401  —— 导入即把 core 加进 sys.path

from core import bangumi, http, identify, jimaku, library, picker, placer, scan as scanner
from core.settings import settings

app = FastAPI(title="JaSubAuto 调试壳", description="生产请用 MoviePilot 插件，这里只用于脱机调试")

UI_HTML = Path(PLUGIN_DIR) / "core" / "ui.html"


def _int_or_none(value):
    return int(value) if value not in (None, "") else None


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    import time
    ts = time.strftime("%m-%d %H:%M", time.localtime(UI_HTML.stat().st_mtime))
    html = (UI_HTML.read_text(encoding="utf-8")
            .replace("__API_BASE__", "/api").replace("__BUILD__", f"调试壳 · {ts}"))
    return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "dry_run": settings.dry_run,
            "jimaku_token_configured": bool(settings.jimaku_api_token),
            "lang_suffix": settings.subtitle_lang_suffix,
            "preferred_sources": settings.preferred_keywords,
            "subtitle_pref": settings.subtitle_pref,
            "strip_annotations": settings.strip_annotations,
            "merge_bilingual": settings.merge_bilingual,
            "keep_japanese_only": settings.keep_japanese_only,
            "proxy": http.describe_proxy()}


@app.get("/api/library")
def api_library(q: str = "", root: str = "") -> dict:
    return library.find_shows(q, root)


@app.get("/api/browse")
def api_browse(path: str, q: str = "") -> dict:
    return library.browse(path, q=q)


@app.get("/api/anilist/search")
def api_anilist(q: str = Query(..., min_length=1)) -> dict:
    hits = identify.search_anilist(q)
    return {"results": [{"anilist_id": h["id"],
                         "romaji": (h.get("title") or {}).get("romaji"),
                         "english": (h.get("title") or {}).get("english"),
                         "native": (h.get("title") or {}).get("native"),
                         "episodes": h.get("episodes"), "year": h.get("seasonYear"),
                         "format": h.get("format")} for h in hits]}


@app.get("/api/resolve")
def api_resolve(tmdb_id: int | None = None, season: int = 1, episode: int = 1,
                title: str = "", bangumi_id: int | None = None) -> dict:
    return vars(identify.resolve(tmdb_id, season, episode, title,
                                 bangumi_ids=bangumi.BangumiIds(show=bangumi_id)))


@app.get("/api/candidates")
def api_candidates(anilist_id: int, episode: int) -> dict:
    entries = jimaku.search_entries(anilist_id)
    if not entries:
        return {"entries": [], "candidates": [], "chosen": None,
                "reason": f"Jimaku 上没有 anilist_id={anilist_id} 的字幕条目"}
    files = jimaku.list_files(entries[0]["id"])
    cands, chosen, reason = picker.pick(files, episode)
    return {"entries": [{"id": e["id"], "name": e.get("name"),
                         "japanese_name": e.get("japanese_name"),
                         "english_name": e.get("english_name"),
                         "last_modified": e.get("last_modified")} for e in entries],
            "entry_id": entries[0]["id"], "file_count": len(files),
            "candidates": [vars(c) for c in cands],
            "chosen": vars(chosen) if chosen else None, "reason": reason}


@app.post("/api/download")
def api_download(payload: dict = Body(...)) -> dict:
    url, name = payload.get("url"), payload.get("name")
    video_path = payload.get("video_path") or ""
    if not url or not name:
        return {"target": "", "written": False, "reason": "缺少 url 或 name"}
    if not video_path:
        return {"target": "", "written": False, "reason": "缺少视频路径"}
    lib_ep = payload.get("library_episode")
    lib_ep = int(lib_ep) if lib_ep not in (None, "") else None
    dry = payload.get("dry_run")
    dry = settings.dry_run if dry is None else bool(dry)
    overwrite = bool(payload.get("overwrite"))

    video, note = placer.resolve_video(video_path, lib_ep)
    if video is None:
        return {"target": "", "written": False, "reason": note, "video": "", "video_note": ""}
    content = None if dry else jimaku.download(url)
    out = placer.place(video_path, name, content, dry_run=dry, library_episode=lib_ep,
                       overwrite=overwrite)
    return {"target": str(out.target), "written": out.written, "reason": out.reason,
            "video": str(out.video) if out.video else "", "video_note": out.video_note,
            "replaced": out.replaced, "merged": out.merged}


@app.post("/api/scan")
def api_scan(payload: dict = Body(...)) -> dict:
    rep = scanner.scan(payload.get("dir") or "",
                       tmdb_id=_int_or_none(payload.get("tmdb_id")),
                       anilist_id=_int_or_none(payload.get("anilist_id")),
                       bangumi_id=_int_or_none(payload.get("bangumi_id")),
                       dry_run=None if payload.get("dry_run") is None else bool(payload["dry_run"]),
                       overwrite=bool(payload.get("overwrite")))
    return {"root": rep.root, "dry_run": rep.dry_run, "total_videos": rep.total_videos,
            "note": rep.note, "summary": rep.summary,
            "episodes": [vars(e) for e in rep.episodes]}


@app.post("/api/jobs")
def api_jobs(payload: dict = Body(...)) -> dict:
    """模拟 MoviePilot 入库事件，验证自动链路。插件里走的是同一个 scan.process_one。"""
    tmdb_id, title = payload.get("tmdb_id"), payload.get("title", "") or ""
    bangumi_id = _int_or_none(payload.get("bangumi_id"))
    whitelist = settings.whitelist_ids
    if whitelist and tmdb_id not in whitelist:
        return {"status": "skipped", "reason": f"tmdb_id={tmdb_id} 不在白名单内"}

    results = []
    for raw in payload.get("files") or []:
        path = str(raw or "")
        mapped = placer.map_path(path) if path else None
        if not path or mapped.suffix.lower() not in placer.VIDEO_EXTS:
            results.append({"video": path, "status": "skipped", "reason": "不是视频文件"})
            continue
        season, episode = scanner.parse_video(mapped)
        try:
            results.append(scanner.process_one(tmdb_id, title, season, episode, path,
                                               bangumi_id=bangumi_id))
        except Exception as exc:
            results.append({"video": path, "status": "error", "reason": str(exc)})
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"status": "processed", "dry_run": settings.dry_run,
            "summary": counts, "results": results}
