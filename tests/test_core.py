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
sys.path.insert(0, str(PROJECT_ROOT / "plugins.v2" / "jasubauto"))

from core import cleaner, identify, library, merge, picker, placer, scan  # noqa: E402
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
    settings.subtitle_pref = "bilingual"
    settings.strip_annotations = True
    settings.merge_bilingual = True
    settings.keep_japanese_only = True
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


def test_pick_prefers_bilingual_by_default():
    """默认偏好是中日双语：日语旁边就有中文，遇到生词不用停下来查。"""
    files = [
        {"name": "[SubsPlease] Show - 05 (1080p) [ABC]_ja.srt", "url": "u1", "size": 1, "last_modified": "x"},
        {"name": "[Nekomoe kissaten] Show [05][Web].JPSC.ass", "url": "u2", "size": 1, "last_modified": "x"},
    ]
    _, chosen, _ = picker.pick(files, 5)
    assert chosen.lang == "ja_zh"


def test_pick_falls_back_to_japanese_when_no_bilingual():
    """双语优先只是把 ja_zh 抬到最前，没有双语时照样选纯日语，不会挑不出来。"""
    files = [
        {"name": "Show.S01E05.WEBRip.Netflix.ja[cc].srt", "url": "u1", "size": 1, "last_modified": "x"},
        {"name": "[SubsPlease] Show - 05 (1080p) [ABC]_ja.srt", "url": "u2", "size": 1, "last_modified": "x"},
    ]
    _, chosen, _ = picker.pick(files, 5)
    assert chosen.lang == "ja"


def test_pick_japanese_mode_demotes_bilingual():
    settings.subtitle_pref = "japanese"
    files = [
        {"name": "[SubsPlease] Show - 05 (1080p) [ABC]_ja.srt", "url": "u1", "size": 1, "last_modified": "x"},
        {"name": "[Nekomoe kissaten] Show [05][Web].JPSC.ass", "url": "u2", "size": 1, "last_modified": "x"},
    ]
    _, chosen, _ = picker.pick(files, 5)
    assert chosen.lang == "ja"


def test_pick_refuses_when_tied():
    """分不出唯一最优就交人工，绝不瞎选。"""
    settings.fansub_whitelist = ""
    files = [{"name": f"[G{i}] Show - 05.srt", "url": f"u{i}", "size": 1, "last_modified": "x"}
             for i in range(3)]
    cands, chosen, reason = picker.pick(files, 5)
    assert len(cands) == 3 and chosen is None and "人工" in reason


# ---------- 清洗说话人标注 ----------

SRT_WITH_LABELS = """1
00:00:01,000 --> 00:00:03,000
（フリーレン）そうだね
（ハイター）ああ

2
00:00:04,000 --> 00:00:05,000
（ドアが開く音）

3
00:00:06,000 --> 00:00:07,000
♪〜

4
00:00:08,000 --> 00:00:09,000
面白い（笑）
"""


def test_clean_srt_strips_speaker_labels():
    out, removed = cleaner.clean(SRT_WITH_LABELS.encode("utf-8"), "x_ja.srt")
    text = out.decode("utf-8")
    assert removed == 4
    assert "（フリーレン）" not in text and "そうだね" in text
    # 整条只有音效/音符的字幕直接丢掉，剩下的序号要重新连续
    assert "（ドアが開く音）" not in text and "♪" not in text
    assert text.startswith("1\n") and "\n2\n" in text and "\n3\n" not in text
    # 行中间的括号是台词的一部分，不能碰
    assert "面白い（笑）" in text


def test_clean_ass_keeps_typesetting_and_chinese():
    ass = (
        "[Events]\n"
        "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,"
        "{\\an8}（アイゼン）行こう\\N（ハイター）うん\n"
        "Dialogue: 0,0:00:06.00,0:00:07.00,Sign,,0,0,0,,{\\p1}m 0 0 l 10 10{\\p0}\n"
        "Dialogue: 0,0:00:08.00,0:00:09.00,Default,,0,0,0,,そうか\\N原来如此\n"
    )
    out, removed = cleaner.clean(ass.encode("utf-8"), "y.JPSC.ass")
    text = out.decode("utf-8")
    assert removed == 2
    assert "（アイゼン）" not in text and "{\\an8}行こう" in text
    # 只有特效标签的排版行不能被当成"清空了"而丢掉——它本来就没有标注可删
    assert "m 0 0 l 10 10" in text
    assert "そうか\\N原来如此" in text


def test_clean_leaves_unknown_format_alone():
    raw = "{100}{200}（誰か）やあ".encode("utf-8")
    assert cleaner.clean(raw, "a.sub") == (raw, 0)


def test_clean_returns_original_bytes_when_nothing_removed():
    """没删到东西就原样返回原始字节：编码万一猜错也不会把文件重写成乱码。"""
    raw = "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n".encode("cp932")
    assert cleaner.clean(raw, "b.srt") == (raw, 0)


def test_place_strips_annotations_on_write(tmp_path):
    video = tmp_path / "Show S01E05.mkv"
    video.write_bytes(b"")
    out = placer.place(str(video), "x_ja.srt", SRT_WITH_LABELS.encode("utf-8"), dry_run=False)
    assert out.written and out.stripped == 4
    assert "（フリーレン）" not in out.target.read_text(encoding="utf-8")


def test_place_can_keep_annotations(tmp_path):
    """开关关掉时必须一个字都不改。"""
    settings.strip_annotations = False
    video = tmp_path / "Show S01E05.mkv"
    video.write_bytes(b"")
    out = placer.place(str(video), "x_ja.srt", SRT_WITH_LABELS.encode("utf-8"), dry_run=False)
    assert out.written and out.stripped == 0
    assert "（フリーレン）" in out.target.read_text(encoding="utf-8")


# ---------- 合成中日双语 ----------

JA_SRT = """1
00:00:01,000 --> 00:00:03,000
そうだね

2
00:00:04,500 --> 00:00:06,000
魔法は面白い

3
00:00:20,000 --> 00:00:21,000
誰もいない
"""

ZH_SRT = """1
00:00:00,900 --> 00:00:03,100
是啊

2
00:00:04,400 --> 00:00:06,200
魔法很有趣

3
00:00:19,800 --> 00:00:21,200
一个人都没有
"""


def _episode(tmp_path, zh_name="Show S01E05.zh-Hans.srt", zh_text=ZH_SRT):
    video = tmp_path / "Show S01E05.mkv"
    video.write_bytes(b"")
    if zh_name:
        (tmp_path / zh_name).write_text(zh_text, encoding="utf-8")
    return video


def test_merge_pairs_lines_by_time_overlap(tmp_path):
    video = _episode(tmp_path)
    out, note = merge.merge_with_chinese(video, JA_SRT.encode("utf-8"), "x_ja.srt")
    text = out.decode("utf-8")
    assert "3/3" in note
    # 日语在上、中文在下：先读日语，读不懂再往下看一眼
    assert "そうだね\n是啊" in text
    assert "魔法は面白い\n魔法很有趣" in text
    # 时间轴以日语那份为准，不能被中文的时间盖掉
    assert "00:00:01,000 --> 00:00:03,000" in text


def test_merge_refuses_when_timelines_disagree(tmp_path):
    """中文字幕来自别的片源时宁可不合并，也不要把台词错位贴上去。"""
    video = _episode(tmp_path, zh_text=ZH_SRT.replace("00:00:", "00:05:"))
    out, note = merge.merge_with_chinese(video, JA_SRT.encode("utf-8"), "x_ja.srt")
    assert out is None and "对不上" in note


def test_merge_ignores_our_own_japanese_subtitle(tmp_path):
    """旁边那份 .ja.srt 是我们自己写的，不能被当成中文来源。"""
    video = _episode(tmp_path, zh_name=None)
    (tmp_path / "Show S01E05.ja.srt").write_text(JA_SRT, encoding="utf-8")
    assert merge.find_chinese_subtitle(video) is None


def test_merge_ignores_untagged_subtitle(tmp_path):
    """没有语言标记的 Show S01E05.srt 不知道是什么语言，不猜。"""
    video = _episode(tmp_path, zh_name=None)
    (tmp_path / "Show S01E05.srt").write_text(ZH_SRT, encoding="utf-8")
    assert merge.find_chinese_subtitle(video) is None


def test_merge_prefers_simplified(tmp_path):
    video = _episode(tmp_path, zh_name="Show S01E05.cht.srt")
    (tmp_path / "Show S01E05.chs.srt").write_text(ZH_SRT, encoding="utf-8")
    assert merge.find_chinese_subtitle(video).name == "Show S01E05.chs.srt"


def test_merge_reads_ass_chinese(tmp_path):
    ass = (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.90,0:00:03.10,Default,,0,0,0,,{\\fad(100,100)}是啊\n"
        "Dialogue: 0,0:00:04.40,0:00:06.20,Default,,0,0,0,,魔法\\N很有趣\n"
        "Dialogue: 0,0:00:04.40,0:00:06.20,Sign,,0,0,0,,本字幕由某字幕组制作\n"
    )
    video = _episode(tmp_path, zh_name="Show S01E05.chs.ass", zh_text=ass)
    out, note = merge.merge_with_chinese(video, JA_SRT.encode("utf-8"), "x_ja.srt")
    text = out.decode("utf-8")
    assert "そうだね\n是啊" in text                 # 特效标签被剥掉
    assert "魔法は面白い\n魔法\n很有趣" in text      # \\N 还原成换行
    assert "字幕组" not in text                     # Sign 样式不是台词，跳过


def test_place_merges_with_chinese_neighbour(tmp_path):
    video = _episode(tmp_path)
    out = placer.place(str(video), "x_ja.srt", JA_SRT.encode("utf-8"), dry_run=False)
    assert out.written and "中日双语" in out.merged
    assert "是啊" in out.target.read_text(encoding="utf-8")


def test_place_writes_pure_japanese_when_no_chinese(tmp_path):
    video = _episode(tmp_path, zh_name=None)
    out = placer.place(str(video), "x_ja.srt", JA_SRT.encode("utf-8"), dry_run=False)
    assert out.written and out.merged == ""
    assert out.target.read_text(encoding="utf-8").count("そうだね") == 1


# ---------- 覆盖已有字幕（只有手动模式能触发） ----------

def test_overwrite_replaces_old_subtitle_of_other_ext(tmp_path):
    """旧的是 .ja.ass、新的是 .ja.srt，不删旧的就会剩两份，播放器挑哪份看不准。"""
    video = _episode(tmp_path, zh_name=None)
    old = tmp_path / "Show S01E05.ja.ass"
    old.write_text("old", encoding="utf-8")
    out = placer.place(str(video), "x_ja.srt", JA_SRT.encode("utf-8"),
                       dry_run=False, overwrite=True)
    assert out.written and out.replaced == "Show S01E05.ja.ass"
    assert not old.exists()
    assert out.target.name == "Show S01E05.ja.srt"


def test_overwrite_dry_run_says_what_it_would_replace(tmp_path):
    video = _episode(tmp_path, zh_name=None)
    (tmp_path / "Show S01E05.ja.ass").write_text("old", encoding="utf-8")
    out = placer.place(str(video), "x_ja.srt", None, dry_run=True, overwrite=True)
    assert not out.written and "将覆盖 Show S01E05.ja.ass" in out.reason


def test_scan_skips_existing_unless_overwrite(tmp_path):
    """自动路径永远不传 overwrite，所以这条跳过逻辑必须是默认行为。"""
    season = tmp_path / "Season 1"
    season.mkdir()
    (season / "Show - S01E05 - 第 5 集.mkv").write_bytes(b"")
    (season / "Show - S01E05 - 第 5 集.ja.srt").write_text("old", encoding="utf-8")

    rep_default = scan.scan(str(season), anilist_id=1, dry_run=True)
    assert rep_default.episodes[0].status == "skipped_existing"

    # 勾了覆盖就不该在这一步被拦下（后面查 Jimaku 会因为没 token 失败，这里只看没被跳过）
    rep_force = scan.scan(str(season), anilist_id=1, dry_run=True, overwrite=True)
    assert rep_force.episodes[0].status != "skipped_existing"


def test_chinese_source_requires_an_external_file(tmp_path):
    """旁边没有中文字幕就到此为止：不抽内嵌轨、不猜，照常写纯日语。"""
    video = _episode(tmp_path, zh_name=None)
    content, name, why = merge.chinese_source(video)
    assert content is None and name == "" and "没有中文字幕" in why


# ---------- 另存一份纯日语 ----------

def test_keeps_pure_japanese_copy(tmp_path):
    """合并后纯日语不能就此消失：另存一份，将来想切回去不用重下。"""
    video = _episode(tmp_path)
    out = placer.place(str(video), "x_ja.srt", JA_SRT.encode("utf-8"), dry_run=False)
    raw = tmp_path / f"Show S01E05.{placer.RAW_TITLE}.ja.srt"
    assert out.raw_target == raw.name and raw.exists()
    text = raw.read_text(encoding="utf-8")
    assert "そうだね" in text and "是啊" not in text        # 另存的那份是纯日语
    assert "是啊" in out.target.read_text(encoding="utf-8")  # 主文件才是双语


def test_raw_copy_is_not_mistaken_for_existing_or_chinese(tmp_path):
    """另存的 <视频名>.原文.ja.srt 既不算"已有日语字幕"，也不能被当成中文来源。"""
    video = _episode(tmp_path, zh_name=None)
    (tmp_path / f"Show S01E05.{placer.RAW_TITLE}.ja.srt").write_text(JA_SRT, encoding="utf-8")
    assert placer.existing_ja_subtitle(video) is None
    assert merge.find_chinese_subtitle(video) is None


def test_keep_japanese_only_can_be_turned_off(tmp_path):
    settings.keep_japanese_only = False
    video = _episode(tmp_path)
    out = placer.place(str(video), "x_ja.srt", JA_SRT.encode("utf-8"), dry_run=False)
    assert out.raw_target == ""
    assert not (tmp_path / f"Show S01E05.{placer.RAW_TITLE}.ja.srt").exists()


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
