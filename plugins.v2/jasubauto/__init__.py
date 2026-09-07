"""JaSubAuto —— MoviePilot V2 插件：给番剧自动补日语字幕，字幕源是 jimaku.cc。

一个插件搞定，不需要额外起服务/容器：
  * 入库完成（TransferComplete）后自动为新入库的剧集补日语字幕
  * 手动页面（浏览媒体库、看候选、单集下载、整部剧批量补扫）由插件自己的 API 提供 HTML

业务逻辑全在 core/ 子包里，不依赖任何 Web 框架，可以脱离 MoviePilot 单独调试
（见仓库里的 service/，那是个只有几十行的调试壳子）。
"""

import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fastapi.responses import HTMLResponse

from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

from .core import identify, jimaku, library, picker, placer, scan as scanner
from .core.settings import configure, settings as core_settings

UI_HTML = Path(__file__).parent / "core" / "ui.html"
API_BASE = "/api/v1/plugin/JaSubAuto"


def _dump(obj: Any, depth: int = 0) -> Any:
    """把事件对象递归转成可打印结构（探针模式用）。

    MediaInfo / TransferInfo 是对象不是 dict，直接打印只能看到 <object at 0x...>。
    """
    if depth > 4:
        return str(obj)
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple, set)):
        return [_dump(x, depth + 1) for x in list(obj)[:20]]
    if isinstance(obj, dict):
        return {str(k): _dump(v, depth + 1) for k, v in obj.items()}
    for attr in ("dict", "model_dump", "to_dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return _dump(fn(), depth + 1)
            except Exception:
                pass
    if hasattr(obj, "__dict__"):
        return {k: _dump(v, depth + 1) for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def _build_stamp() -> str:
    """版本 + ui.html 的修改时间，用来确认部署到底有没有生效。"""
    try:
        ts = time.strftime("%m-%d %H:%M", time.localtime(UI_HTML.stat().st_mtime))
    except OSError:
        ts = "?"
    return f"v{JaSubAuto.plugin_version} · {ts}"


def _attr(obj, name):
    """dict 和对象两种形态都能取字段。MoviePilot 的事件里两种都出现过。"""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _tmdb_id(mediainfo):
    """取 tmdb_id。

    实测 mediainfo 里 `media_id` 是字符串 '209867'，且 `source='themoviedb'`；
    `tmdb_id` 字段可能也在（日志被截断没看全），所以两个都试，优先真字段。
    """
    raw = _attr(mediainfo, "tmdb_id")
    if raw is None and str(_attr(mediainfo, "source") or "").lower() in ("themoviedb", "tmdb"):
        raw = _attr(mediainfo, "media_id")
    try:
        return int(str(raw)) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _path_of(item):
    """把 fileitem / 路径字符串统一成路径字符串。"""
    if item is None:
        return ""
    if isinstance(item, str):
        return item
    return str(_attr(item, "path") or "")


def _target_files(transferinfo) -> list:
    """从 transferinfo 里取**入库之后**的视频文件路径。

    注意 `transferinfo.fileitem` 是**下载源**路径（/媒体/下载/...），不是入库目标，
    所以它排在最后，只在别的字段都取不到时才用。
    """
    for name in ("file_list_new", "file_list"):
        value = _attr(transferinfo, name)
        if value:
            paths = [_path_of(v) for v in (value if isinstance(value, (list, tuple)) else [value])]
            out = [p for p in paths if p and Path(p).suffix.lower() in placer.VIDEO_EXTS]
            if out:
                return out
    for name in ("target_item", "target_path"):
        p = _path_of(_attr(transferinfo, name))
        if p and Path(p).suffix.lower() in placer.VIDEO_EXTS:
            return [p]
    return []


class JaSubAuto(_PluginBase):
    plugin_name = "日语字幕补全（Jimaku）"
    plugin_desc = "入库后自动从 jimaku.cc 补日语字幕，并提供手动挑选/整部剧批量补扫的网页"
    plugin_icon = "jasubauto.png"          # 解析到本仓库的 icons/ 下
    plugin_version = "0.7.0"
    plugin_author = "dewe001"
    author_url = "https://github.com/dewe001"
    plugin_config_prefix = "jasubauto_"
    plugin_order = 20
    auth_level = 1

    _enabled = False
    _probe_only = True

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        probe = config.get("probe_only")
        # 缺省必须是"开"：没核对过事件字段之前不该真的动媒体库
        self._probe_only = True if probe is None else bool(probe)

        configure({
            "jimaku_api_token": config.get("jimaku_api_token", ""),
            "dry_run": config.get("dry_run", True),
            "series_whitelist": config.get("series_whitelist", ""),
            "subtitle_lang_suffix": config.get("lang_suffix", "") or "ja",
            "fansub_whitelist": config.get("fansub_whitelist", ""),
            "subtitle_pref": config.get("subtitle_pref", "") or "bilingual",
            "strip_annotations": config.get("strip_annotations", True),
            "merge_bilingual": config.get("merge_bilingual", True),
            "keep_japanese_only": config.get("keep_japanese_only", True),
            "media_roots": config.get("media_roots", ""),
            "data_dir": self._data_dir(),
        })

    def _data_dir(self) -> str:
        """映射表（7.5MB）落在插件的数据目录，插件更新不会被清掉。"""
        getter = getattr(self, "get_data_path", None)
        if callable(getter):
            try:
                return str(getter())
            except Exception:
                pass
        return str(Path(__file__).parent / "data")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    # ---------- 插件自己的 API：手动页面 + 数据接口 ----------

    def get_api(self) -> List[Dict[str, Any]]:
        """MoviePilot 用 router.add_api_route(**api) 注册，所以 response_class 这类
        FastAPI 参数可以直接透传——手动页面因此能由插件自己返回 HTML，不必另起服务。

        统一用 apikey 鉴权：页面是在浏览器里直接打开的，带不了 Bearer 头。
        """
        common = {"auth": "apikey"}
        return [
            {**common, "path": "/ui", "endpoint": self.api_ui, "methods": ["GET"],
             "response_class": HTMLResponse, "summary": "日语字幕手动页面"},
            {**common, "path": "/health", "endpoint": self.api_health, "methods": ["GET"],
             "summary": "状态"},
            {**common, "path": "/library", "endpoint": self.api_library, "methods": ["GET"],
             "summary": "在番剧库里搜剧"},
            {**common, "path": "/browse", "endpoint": self.api_browse, "methods": ["GET"],
             "summary": "列出指定目录的子目录（兜底）"},
            {**common, "path": "/anilist/search", "endpoint": self.api_anilist, "methods": ["GET"],
             "summary": "按标题搜 AniList"},
            {**common, "path": "/resolve", "endpoint": self.api_resolve, "methods": ["GET"],
             "summary": "TMDB 季集号换算成 AniList"},
            {**common, "path": "/candidates", "endpoint": self.api_candidates, "methods": ["GET"],
             "summary": "列出某集的候选字幕"},
            {**common, "path": "/download", "endpoint": self.api_download, "methods": ["POST"],
             "summary": "下载并落盘单个字幕"},
            {**common, "path": "/scan", "endpoint": self.api_scan, "methods": ["POST"],
             "summary": "批量补扫一个番剧文件夹"},
        ]

    def api_ui(self) -> HTMLResponse:
        """返回手动页面。

        禁缓存是必须的：改完插件重新部署后，浏览器拿旧 HTML 会让人误以为"改动没生效"，
        排查方向直接跑偏。页面右上角还打了版本戳，一眼能看出加载的是哪一版。
        """
        html = (UI_HTML.read_text(encoding="utf-8")
                .replace("__API_BASE__", API_BASE)
                .replace("__BUILD__", _build_stamp()))
        return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})

    def api_health(self) -> dict:
        return {
            "ok": True,
            "dry_run": core_settings.dry_run,
            "jimaku_token_configured": bool(core_settings.jimaku_api_token),
            "lang_suffix": core_settings.subtitle_lang_suffix,
            "preferred_sources": core_settings.preferred_keywords,
            "subtitle_pref": core_settings.subtitle_pref,
            "strip_annotations": core_settings.strip_annotations,
            "merge_bilingual": core_settings.merge_bilingual,
            "keep_japanese_only": core_settings.keep_japanese_only,
        }

    def api_library(self, q: str = "", root: str = "") -> dict:
        """按关键字在番剧库里找剧。q 为空只报总数，不铺满一屏。"""
        return library.find_shows(q, root)

    def api_browse(self, path: str, q: str = "") -> dict:
        """兜底：手工指定目录时列出它的子目录。"""
        return library.browse(path, q=q)

    def api_anilist(self, q: str) -> dict:
        hits = identify.search_anilist(q)
        return {"results": [{"anilist_id": h["id"],
                             "romaji": (h.get("title") or {}).get("romaji"),
                             "english": (h.get("title") or {}).get("english"),
                             "native": (h.get("title") or {}).get("native"),
                             "episodes": h.get("episodes"), "year": h.get("seasonYear"),
                             "format": h.get("format")} for h in hits]}

    def api_resolve(self, tmdb_id: int = None, season: int = 1,
                    episode: int = 1, title: str = "") -> dict:
        return vars(identify.resolve(tmdb_id, season, episode, title))

    def api_candidates(self, anilist_id: int, episode: int) -> dict:
        entries = jimaku.search_entries(anilist_id)
        if not entries:
            return {"entries": [], "candidates": [], "chosen": None,
                    "reason": f"Jimaku 上没有 anilist_id={anilist_id} 的字幕条目"}
        files = jimaku.list_files(entries[0]["id"])
        cands, chosen, reason = picker.pick(files, episode)
        return {
            "entries": [{"id": e["id"], "name": e.get("name"),
                         "japanese_name": e.get("japanese_name"),
                         "english_name": e.get("english_name"),
                         "last_modified": e.get("last_modified")} for e in entries],
            "entry_id": entries[0]["id"], "file_count": len(files),
            "candidates": [vars(c) for c in cands],
            "chosen": vars(chosen) if chosen else None, "reason": reason,
        }

    def api_download(self, payload: dict) -> dict:
        url, name = payload.get("url"), payload.get("name")
        video_path = payload.get("video_path") or ""
        if not url or not name:
            return {"target": "", "written": False, "reason": "缺少 url 或 name"}
        if not video_path:
            return {"target": "", "written": False, "reason": "缺少视频路径"}
        lib_ep = payload.get("library_episode")
        lib_ep = int(lib_ep) if lib_ep not in (None, "") else None
        dry = payload.get("dry_run")
        dry = core_settings.dry_run if dry is None else bool(dry)
        # 覆盖只可能从手动页面来：人看着候选列表点的，和自动入库那条路无关
        overwrite = bool(payload.get("overwrite"))

        # 先确认能定位到唯一视频，定位不了就别浪费 Jimaku 配额（限速 25/分钟）
        video, note = placer.resolve_video(video_path, lib_ep)
        if video is None:
            return {"target": "", "written": False, "reason": note, "video": "", "video_note": ""}
        content = None if dry else jimaku.download(url)
        out = placer.place(video_path, name, content, dry_run=dry, library_episode=lib_ep,
                           overwrite=overwrite)
        return {"target": str(out.target), "written": out.written, "reason": out.reason,
                "video": str(out.video) if out.video else "", "video_note": out.video_note,
                "replaced": out.replaced, "merged": out.merged}

    def api_scan(self, payload: dict) -> dict:
        rep = scanner.scan(
            payload.get("dir") or "",
            tmdb_id=int(payload["tmdb_id"]) if payload.get("tmdb_id") else None,
            anilist_id=int(payload["anilist_id"]) if payload.get("anilist_id") else None,
            dry_run=None if payload.get("dry_run") is None else bool(payload["dry_run"]),
            overwrite=bool(payload.get("overwrite")),
        )
        return {"root": rep.root, "dry_run": rep.dry_run, "total_videos": rep.total_videos,
                "note": rep.note, "summary": rep.summary,
                "episodes": [vars(e) for e in rep.episodes]}

    # ---------- 配置表单与详情页 ----------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        def col(md, comp, props):
            return {"component": "VCol", "props": {"cols": 12, "md": md},
                    "content": [{"component": comp, "props": props}]}

        return [{
            "component": "VForm",
            "content": [
                {"component": "VRow", "content": [
                    col(4, "VSwitch", {"model": "enabled", "label": "启用插件"}),
                    col(4, "VSwitch", {"model": "probe_only",
                                       "label": "探针模式（只打日志，不处理）"}),
                    col(4, "VSwitch", {"model": "dry_run",
                                       "label": "试运行（不实际写入字幕文件）"}),
                ]},
                {"component": "VRow", "content": [
                    col(12, "VTextField", {"model": "jimaku_api_token",
                                           "label": "Jimaku API Token",
                                           "placeholder": "在 https://jimaku.cc/profile 申请"}),
                ]},
                {"component": "VRow", "content": [
                    col(6, "VTextField", {"model": "media_roots", "label": "媒体库根目录（逗号分隔）",
                                          "placeholder": "/媒体"}),
                    col(6, "VTextField", {"model": "series_whitelist",
                                          "label": "剧集白名单 tmdb_id（逗号分隔，留空=不限制）",
                                          "placeholder": "调试期建议只填一两部"}),
                ]},
                {"component": "VRow", "content": [
                    col(6, "VTextField", {"model": "fansub_whitelist", "label": "片源偏好（逗号分隔，优先级从高到低）",
                                          "placeholder": "Netflix,Amazon,SubsPlease,Moozzi2"}),
                    col(6, "VTextField", {"model": "lang_suffix", "label": "字幕语言后缀",
                                          "placeholder": "ja"}),
                ]},
                {"component": "VRow", "content": [
                    col(12, "VSwitch", {"model": "keep_japanese_only",
                                        "label": "合并成双语后，另存一份纯日语（<视频名>.原文.ja.srt）"}),
                ]},
                {"component": "VRow", "content": [
                    col(4, "VSelect", {"model": "subtitle_pref", "label": "字幕偏好",
                                       "items": [{"title": "中日双语优先（查词方便）", "value": "bilingual"},
                                                 {"title": "纯日语优先", "value": "japanese"}]}),
                    col(4, "VSwitch", {"model": "strip_annotations",
                                       "label": "去掉说话人标注与音效描述"}),
                    col(4, "VSwitch", {"model": "merge_bilingual",
                                       "label": "与旁边的中文字幕合成双语"}),
                ]},
                {"component": "VRow", "content": [{
                    "component": "VCol", "props": {"cols": 12},
                    "content": [{"component": "VAlert", "props": {
                        "type": "info", "variant": "tonal",
                        "text": "首次使用请保持「探针模式」和「试运行」开启：先触发一次整理，"
                                "在日志里核对事件字段和将要写入的路径，确认无误再逐个关掉。"
                                "已存在的字幕文件永远不会被覆盖。"}}]}]},
            ],
        }], {"enabled": False, "probe_only": True, "dry_run": True,
             "jimaku_api_token": "", "media_roots": "", "series_whitelist": "",
             "fansub_whitelist": "Netflix,Amazon,SubsPlease,Moozzi2", "lang_suffix": "ja",
             "subtitle_pref": "bilingual", "strip_annotations": True,
             "merge_bilingual": True, "keep_japanese_only": True}

    def get_page(self) -> List[dict]:
        """详情页只放一个入口，真正的手动界面是插件 API 返回的那张 HTML 页。"""
        try:
            from app.core.config import settings as mp_settings
            key = getattr(mp_settings, "API_TOKEN", "") or ""
        except Exception:
            key = ""
        url = f"{API_BASE}/ui" + (f"?apikey={key}" if key else "")
        return [{
            "component": "VCard",
            "props": {"variant": "tonal"},
            "content": [
                {"component": "VCardTitle", "text": "手动挑字幕 / 整部剧批量补扫"},
                {"component": "VCardText", "content": [
                    {"component": "div", "props": {"class": "mb-2"},
                     "text": "浏览媒体库按中文剧名选剧、查看候选字幕、单集下载、"
                             "整部剧一次性补齐缺失的日语字幕，都在这个页面上："},
                    {"component": "a", "props": {"href": url, "target": "_blank",
                                                 "class": "text-primary text-h6"},
                     "text": "打开日语字幕手动页面"},
                    {"component": "div", "props": {"class": "mt-3 text-caption"},
                     "text": "当前：%s，%s" % (
                         "探针模式（不处理）" if self._probe_only else "自动处理已启用",
                         "试运行（不写盘）" if core_settings.dry_run else "会实际写入字幕")},
                ]},
            ],
        }]

    def stop_service(self):
        pass

    # ---------- 自动模式：入库完成事件 ----------

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        """入库完成。整个处理放到后台线程，绝不阻塞 MoviePilot 的入库流程。"""
        if not self._enabled:
            return
        try:
            data = event.event_data or {}

            if self._probe_only:
                self._probe(data)
                return

            threading.Thread(target=self._handle, args=(data,), daemon=True).start()
        except Exception as exc:
            logger.error("【日语字幕】处理事件出错（已忽略，不影响入库）：%s" % exc)

    def _probe(self, data: dict):
        """探针：只打我们真正要用的字段，一行一条。

        早期版本整个 dump mediainfo/transferinfo，结果日志一行几千字被截断，
        恰好把入库目标路径截没了——那正是唯一要确认的东西。
        """
        logger.info("【日语字幕】═══ 探针 ═══ 顶层键：%s" % list(data.keys()))

        mi, ti = data.get("mediainfo"), data.get("transferinfo")
        logger.info("【日语字幕】mediainfo: tmdb_id=%r media_id=%r type=%r title=%r"
                    % (_attr(mi, "tmdb_id"), _attr(mi, "media_id"),
                       _attr(mi, "type"), _attr(mi, "title")))
        logger.info("【日语字幕】→ 解析出的 tmdb_id = %r" % _tmdb_id(mi))

        meta = data.get("meta")
        logger.info("【日语字幕】meta: begin_season=%r begin_episode=%r end_episode=%r"
                    % (_attr(meta, "begin_season"), _attr(meta, "begin_episode"),
                       _attr(meta, "end_episode")))

        ti_keys = list(ti.keys()) if isinstance(ti, dict) else [
            k for k in dir(ti) if not k.startswith("_") and not callable(getattr(ti, k, None))]
        logger.info("【日语字幕】transferinfo 字段：%s" % ti_keys)
        for name in ("file_list_new", "file_list", "target_item", "target_diritem",
                     "target_path", "fileitem"):
            if _attr(ti, name) is not None:
                logger.info("【日语字幕】transferinfo.%s = %s" % (name, _dump(_attr(ti, name), 3)))

        files = _target_files(ti)
        logger.info("【日语字幕】→ 判定为入库后的视频文件（%d 个）：%s" % (len(files), files))
        for f in files:
            season, episode = scanner.parse_video(Path(f))
            logger.info("【日语字幕】→ %s 解析为 第 %s 季 第 %s 集（媒体库口径）"
                        % (Path(f).name, season, episode))
        logger.info("【日语字幕】═══ 探针模式，不处理。核对无误后关闭探针模式即可自动补字幕 ═══")

    def _handle(self, data: dict):
        """真正干活的部分。失败一律只记日志。"""
        try:
            mediainfo, transferinfo = data.get("mediainfo"), data.get("transferinfo")
            tmdb_id = _tmdb_id(mediainfo)
            title = _attr(mediainfo, "title") or _attr(mediainfo, "org_string") or ""
            files = _target_files(transferinfo)
            if not files:
                logger.info("【日语字幕】没找到入库后的视频文件，跳过")
                return

            whitelist = core_settings.whitelist_ids
            if whitelist and tmdb_id not in whitelist:
                logger.info("【日语字幕】tmdb_id=%s 不在白名单内，跳过" % tmdb_id)
                return

            for path in files:
                p = Path(path)
                season, episode = scanner.parse_video(p)
                result = scanner.process_one(tmdb_id, title, season, episode, path)
                logger.info("【日语字幕】%s → %s：%s"
                            % (p.name, result.get("status"), result.get("reason", "")))
        except Exception as exc:
            logger.error("【日语字幕】补字幕失败（已忽略）：%s" % exc)
