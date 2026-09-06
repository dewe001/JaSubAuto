"""核心逻辑的回归测试：pytest tests/

**完全不联网**：Jimaku / AniList 不调用，ID 映射表用 tests/fixtures 里的三行迷你版
（真表 7.5MB 不进仓库，直接用它会让新克隆的仓库跑测试时去下载）。
用例大多来自实际踩过的坑，注释里写明是哪一个。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "plugins.v2" / "jimakutrigger"))

from core import identify, library, picker, placer, scan  # noqa: E402
from core.settings import settings  # noqa: E402


FIXTURE_MAPPING = Path(__file__).parent / "fixtures" / "anime-list-mini.json"


@pytest.fixture(autouse=True)
def _settings(tmp_path_factory):
    # 用独立的临时目录，别放进用例自己的 tmp_path——
    # 媒体库搜索的用例会把 tmp_path 下的每个子目录都当成一部剧
    data_dir = tmp_path_factory.mktemp("mapping")
    shutil.copy(FIXTURE_MAPPING, data_dir / "anime-list-full.json")
    settings.data_dir = data_dir
    settings.anime_lists_refresh_days = 36500
    identify._index = None                          # 索引是模块级缓存，每个用例都得清
    settings.subtitle_lang_suffix = "ja"
    settings.fansub_whitelist = "Netflix,Amazon,SubsPlease,Moozzi2"
    settings.media_roots = ""
    settings.dry_run = True
    yield


# ---------- 集号解析 ----------

@pytest.mark.parametrize("name, want", [
    # 用户媒体库的真实命名：同时含 S01E36 和「第 36 集」，S01E36 必须优先
    ("葬送的芙莉莲 - S01E36 - 第 36 集.mkv", (1, 36)),
    ("葬送的芙莉莲 S01E05.mkv", (1, 5)),
    ("某剧 - S02E09 - 第 9 集.mkv", (2, 9)),
    # 解析不出集号的（特典/剧场版）必须返回 None 而不是瞎猜
    ("番外 SP.mkv", (None, None)),
])
def test_parse_video(name, want):
    assert scan.parse_video(Path("/媒体/日番/某剧/Season 1") / name) == want


@pytest.mark.parametrize("name, want", [
    ("[SubsPlease] Sousou no Frieren - 05 (1080p) [8E3F8FA5]_ja.srt", 5),
    # 方括号集号：字幕组常用，曾经漏解析 31 个文件
    ("[Nekomoe kissaten] Sousou no Frieren [01][Web].JPSC.ass", 1),
    # CRC 不能被当成集号
    ("[Group] Show [8E3F8FA5].srt", None),
    ("Sousou no Frieren - 18 「一級魔法使い選抜試験」 (NTV 1920x1080 x264 AAC).srt", 18),
])
def test_parse_episode(name, want):
    assert picker.parse_episode(name) == want


# ---------- 语言纯度 ----------

@pytest.mark.parametrize("name, lang", [
    ("[SubsPlease] Show - 05 (1080p) [ABC]_ja.srt", "ja"),
    ("Show.S01E05.WEBRip.Netflix.ja[cc].srt", "ja_cc"),
    ("Show.S01E05.WEBRip.Amazon.ja-jp[sdh].srt", "ja_cc"),
    ("[Nekomoe kissaten] Show [01][Web].JPSC.ass", "ja_zh"),
    ("[KitaujiSub] Show - 01 [CHS, JPN].ass", "ja_zh"),
    ("[SubsPlease] Show - 05 (1080p) [ABC]_ja-en.ass", "ja_other"),
    ("Show - 05 (AT-X 1280x720 x264 AAC).srt", "unknown"),
    # "Accel" 里的 cc 不能被当成 CC 标记
    ("Accel World - 05.srt", "unknown"),
])
def test_classify_language(name, lang):
    assert picker.classify_language(name)[0] == lang


def test_pick_prefers_clean_japanese_over_cc():
    """用户是拿来学日语的：干净纯台词 > 含听障注释。"""
    files = [
        {"name": "Show.S01E05.WEBRip.Netflix.ja[cc].srt", "url": "u1", "size": 1, "last_modified": "2026-01-01"},
        {"name": "[SubsPlease] Show - 05 (1080p) [ABC]_ja.srt", "url": "u2", "size": 1, "last_modified": "2026-01-01"},
    ]
    _, chosen, _ = picker.pick(files, 5)
    assert chosen.name.startswith("[SubsPlease]")


def test_pick_refuses_when_tied():
    """分不出唯一最优就交人工，绝不瞎选。"""
    settings.fansub_whitelist = ""
    files = [{"name": f"[G{i}] Show - 05.srt", "url": f"u{i}", "size": 1, "last_modified": "x"}
             for i in range(3)]
    cands, chosen, reason = picker.pick(files, 5)
    assert len(cands) == 3 and chosen is None and "人工" in reason


# ---------- 集号换算 ----------

def test_offset_conversion():
    """TMDB 连续编号 → AniList 分季编号。媒体库 S01E37 = 第二季第 9 集。"""
    r = identify.resolve(209867, 1, 37)
    assert (r.anilist_id, r.episode, r.confident) == (182255, 9, True)


def test_season_zero_goes_to_manual():
    """特典编号体系不可靠，一律交人工，绝不配正片字幕。"""
    r = identify.resolve(209867, 0, 1)
    assert r.anilist_id is None


# ---------- 落盘 ----------

def test_never_overwrites_existing_subtitle(tmp_path):
    video = tmp_path / "Show S01E05.mkv"
    video.write_bytes(b"")
    (tmp_path / "Show S01E05.ja.ass").write_text("existing", encoding="utf-8")
    # 已有的是 .ass，要写的是 .srt：只比对同扩展名会漏，导致多出一份重复字幕
    out = placer.place(str(video), "x_ja.srt", b"data", dry_run=False)
    assert not out.written and "已有日语字幕" in out.reason


def test_directory_is_not_treated_as_filename(tmp_path):
    """给目录时不能算出「目录同级的兄弟文件」（Path.with_name 的坑）。"""
    (tmp_path / "Season 1").mkdir()
    out = placer.place(str(tmp_path / "Season 1"), "x_ja.srt", b"d", dry_run=True)
    assert not out.written and out.target == Path(".")


def test_directory_plus_episode_finds_the_video(tmp_path):
    season = tmp_path / "Season 1"
    season.mkdir()
    for n in (4, 5):
        (season / f"Show - S01E0{n} - 第 {n} 集.mkv").write_bytes(b"")
    out = placer.place(str(season), "x_ja.srt", None, dry_run=True, library_episode=5)
    assert out.video.name == "Show - S01E05 - 第 5 集.mkv"
    assert out.target.name == "Show - S01E05 - 第 5 集.ja.srt"


# ---------- 媒体库搜索 ----------

def _make_library(root: Path, names: list[str]) -> None:
    for n in names:
        (root / n / "Season 1").mkdir(parents=True)
        (root / n / "Season 1" / f"{n} S01E01.mkv").write_bytes(b"")


def test_empty_query_lists_nothing(tmp_path):
    """番剧库动辄几百部，一进页面就铺出来没法看：不输关键字只报总数。"""
    _make_library(tmp_path, ["剧A", "剧B", "剧C"])
    settings.media_roots = str(tmp_path)
    d = library.find_shows("")
    assert d["entries"] == [] and "共 3 部" in d["note"]


def test_query_filters_and_keeps_path(tmp_path):
    _make_library(tmp_path, ["葬送的芙莉莲 (2023)", "咒术回战", "电锯人"])
    settings.media_roots = str(tmp_path)
    d = library.find_shows("芙莉莲")
    assert len(d["entries"]) == 1
    # 有 path 才能点「选它」——不能因为没统计集数或没 tmdb_id 就少了它
    assert d["entries"][0]["path"].endswith("葬送的芙莉莲 (2023)")


def test_nfo_supplies_tmdb_id(tmp_path):
    _make_library(tmp_path, ["葬送的芙莉莲 (2023)"])
    (tmp_path / "葬送的芙莉莲 (2023)" / "tvshow.nfo").write_text(
        '<?xml version="1.0"?><tvshow><title>葬送的芙莉莲</title><year>2023</year>'
        '<uniqueid type="tmdb">209867</uniqueid></tvshow>', encoding="utf-8")
    settings.media_roots = str(tmp_path)
    e = library.find_shows("芙莉莲")["entries"][0]
    assert e["tmdb_id"] == 209867 and e["title"] == "葬送的芙莉莲"


def test_search_does_not_leak_outside_configured_roots(tmp_path):
    """番剧库配 /媒体/日番，下载目录和电影库不该出现在结果里。"""
    (tmp_path / "日番").mkdir()
    _make_library(tmp_path / "日番", ["某番剧"])
    (tmp_path / "下载" / "某下载").mkdir(parents=True)
    settings.media_roots = str(tmp_path / "日番")
    assert library.find_shows("某下载")["entries"] == []
    assert len(library.find_shows("某番剧")["entries"]) == 1
