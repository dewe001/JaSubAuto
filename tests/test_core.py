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

from core import bangumi, cleaner, http, identify, jimaku, library, merge, picker, placer, scan, trace  # noqa: E402
from core.settings import settings  # noqa: E402


FIXTURE_MAPPING = Path(__file__).parent / "fixtures" / "anime-list-mini.json"
FIXTURE_BANGUMI_MAP = Path(__file__).parent / "fixtures" / "bangumi-map-mini.json"


@pytest.fixture(autouse=True)
def _settings(tmp_path_factory, monkeypatch):
    # 用独立的临时目录，别放进用例自己的 tmp_path——
    # 媒体库搜索的用例会把 tmp_path 下的每个子目录都当成一部剧
    data_dir = tmp_path_factory.mktemp("mapping")
    shutil.copy(FIXTURE_MAPPING, data_dir / "anime-list-full.json")
    shutil.copy(FIXTURE_BANGUMI_MAP, data_dir / "bangumi-map.json")
    settings.data_dir = data_dir
    settings.anime_lists_refresh_days = 36500
    identify._index = None                          # 索引是模块级缓存，每个用例都得清
    identify._id_index = None
    bangumi.reset_cache()
    http.reset()                                    # 「连不上暂时跳过」的记录也是模块级的
    settings.subtitle_lang_suffix = "ja"
    settings.fansub_whitelist = "Netflix,Amazon,SubsPlease,Moozzi2"
    settings.media_roots = ""
    settings.dry_run = True
    settings.subtitle_pref = "bilingual"
    settings.strip_annotations = True
    settings.merge_bilingual = True
    settings.keep_japanese_only = True
    settings.proxy = ""
    # 单测不许联网：Bangumi API、Jimaku 标题搜索、AniList 开播年份一律换成假数据
    monkeypatch.setattr(bangumi, "_api_get", _fake_bangumi_api)
    monkeypatch.setattr(jimaku, "search_by_title", lambda q: list(JIMAKU_ENTRIES))
    monkeypatch.setattr(identify, "anilist_start_year", lambda aid: ANILIST_YEARS.get(aid))
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


# ---------- 同分候选按规则挑 ----------

def _f(name, modified="2026-06-24", size=1):
    return {"name": name, "url": name, "size": size, "last_modified": modified}


def test_pick_same_release_in_two_formats_takes_srt():
    """真实数据：芙莉莲第二季 10 集全卡在同一份字幕的 ass + srt 同分上。"""
    settings.fansub_whitelist = ""
    files = [_f("[NanakoRaws] Sousou no Frieren S2 - 03 (NTV 1080p HEVC AAC).ass", size=88398),
             _f("[NanakoRaws] Sousou no Frieren S2 - 03 (NTV 1080p HEVC AAC).srt", size=22400)]
    _, chosen, reason = picker.pick(files, 3)
    assert chosen.name.endswith(".srt") and ".srt" in reason


KAMIINA = "Kamiina Botan, Yoeru Sugata wa Yuri no Hana"


def _kamiina_files():
    """真实数据的缩影：上伊那牡丹三个组都出了中日双语，12 集全部同分。KitaujiSub 缺第 12 集。"""
    files = []
    for ep in range(1, 13):
        files.append(_f(f"[Haruhana] {KAMIINA} - {ep:02d} [WebRip][HEVC-10bit 1080p][CHS, JPN].ass", "2026-06-20"))
        files.append(_f(f"[Haruhana] {KAMIINA} - {ep:02d} [WebRip][HEVC-10bit 1080p][JPN].ass", "2026-06-20"))
        files.append(_f(f"[Nekomoe kissaten&LoliHouse] {KAMIINA} - {ep:02d} "
                        f"[WebRip 1080p HEVC-10bit AAC ASSx2][CHS, JPN].ass", "2026-06-01"))
        if ep < 12:
            files.append(_f(f"[KitaujiSub] {KAMIINA} [{ep:02d}][WebRip][HEVC_AAC][CHS, JPN].ass", "2026-09-01"))
    return files


def test_pick_keeps_one_group_for_the_whole_series():
    """同分时按组比而不是按单个文件比：否则第 1 集 A 组、第 2 集 B 组，字幕风格来回跳。
    KitaujiSub 更新最晚但缺一集，Haruhana 与 Nekomoe 都齐，Haruhana 更新更晚。"""
    settings.fansub_whitelist = ""
    files = _kamiina_files()
    picked = {picker.release_group(picker.pick(files, ep)[1].name) for ep in range(1, 13)}
    assert picked == {"haruhana"}
    _, _, reason = picker.pick(files, 1)
    assert "Haruhana" in reason and "整部剧" in reason


def test_pick_prefers_group_that_covers_more_episodes():
    settings.fansub_whitelist = ""
    files = [_f(f"[Old] Show - {e:02d} [CHS, JPN].ass", "2026-01-01") for e in range(1, 13)]
    files += [_f(f"[New] Show - {e:02d} [CHS, JPN].ass", "2026-09-01") for e in range(1, 7)]
    _, chosen, reason = picker.pick(files, 3)
    assert chosen.name.startswith("[Old]") and "12 集" in reason


def test_pick_follows_the_video_release_group():
    """自动模式能拿到下载时的原始文件名，和视频同组的字幕时间轴最可能对得上。"""
    settings.fansub_whitelist = ""
    hint = f"[KitaujiSub] {KAMIINA} [05][WebRip][HEVC_AAC][CHS].mp4"
    _, chosen, reason = picker.pick(_kamiina_files(), 5, hint=hint)
    assert chosen.name.startswith("[KitaujiSub]") and "KitaujiSub" in reason


def test_pick_tie_is_deterministic():
    """什么都一样时也要选得出来，而且不随 Jimaku 返回顺序变。"""
    settings.fansub_whitelist = ""
    files = [_f(f"[G{i}] Show - 05.srt") for i in range(3)]
    picks = {picker.pick(order, 5)[1].name for order in (files, files[::-1])}
    assert picks == {"[G0] Show - 05.srt"}


def test_release_group_is_stable_across_episodes():
    assert picker.release_group("Kinomi Master - 01 「唯一の素材」 (MX 1920x1080 x264 AAC).srt") == \
        picker.release_group("Kinomi Master - 02 「未完の輝き」 (MX 1920x1080 x264 AAC).ass")
    assert picker.release_group(
        "Kamiina.Botan.S01E03.720p.ABEMA.WEB-DL.JPN.AAC2.0.H.264-ToonsHub.ass") == "toonshub"


@pytest.mark.parametrize("name, batch", [
    # 真实数据：1-4 集合在一个文件里，曾被当成第 1 集的候选
    ("[KitaujiSub] Sousou no Frieren - 01-04 TVSP [CHS, JPN].ass", True),
    ("[Group] Show [01-12][1080p].ass", True),
    ("Show Complete Batch.srt", True),
    # 季名里的 Season 不是合集标记
    ("Sousou no Frieren 2nd Season - 01.srt", False),
    ("[NanakoRaws] Sousou no Frieren S2 - 03 (NTV 1080p HEVC AAC).srt", False),
    # 日期不是集号范围
    ("Show 2024-06-18 - 05.srt", False),
])
def test_is_batch(name, batch):
    assert picker.is_batch(name) is batch


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


# ---------- Bangumi ID（Jellyfin 的 Bangumi 插件刮削的库） ----------
# 数据取自 2026-09-12 的真实 API 返回，裁掉了用不到的字段

def _eps(sorts):
    return [{"id": 100000 + i, "type": 0, "sort": s, "ep": i + 1} for i, s in enumerate(sorts)]


BGM_EPISODES = {
    400602: _eps(range(1, 29)),        # 芙莉莲第一季：sort 与 ep 相同
    515759: _eps(range(29, 39)),       # 芙莉莲第二季：sort 29~38，ep 1~10
    369304: _eps(range(25, 48)),       # 咒术回战第二季：sort 25~47，ep 1~23
    5000: _eps(range(10, 22)),         # 虚构：sort 10~21，和 ep 1~12 有重叠
    6000: _eps(range(1, 13)),
    777: _eps(range(1, 13)),
}
BGM_ROUTES = {
    "/v0/subjects/400602": {"id": 400602, "name": "葬送のフリーレン", "date": "2023-09-29",
                            "infobox": [{"key": "别名", "value": [{"v": "Sousou no Frieren"}]}]},
    "/v0/subjects/515759": {"id": 515759, "name": "葬送のフリーレン 第2期", "date": "2026-01-16",
                            "infobox": [{"key": "别名", "value": [
                                {"v": "Sousou no Frieren 2nd Season"},
                                {"v": "Frieren: Beyond Journey's End Season 2"}]}]},
    "/v0/subjects/6000": {"id": 6000, "name": "葬送", "date": "2026-01-01", "infobox": []},
    "/v0/subjects/400602/subjects": [{"id": 515759, "type": 2, "relation": "续集"},
                                     {"id": 459283, "type": 2, "relation": "衍生"},
                                     {"id": 477900, "type": 3, "relation": "原声集"}],
    "/v0/subjects/515759/subjects": [{"id": 400602, "type": 2, "relation": "前传"}],
    "/v0/episodes/9001": {"id": 9001, "type": 0, "sort": 36, "ep": 8, "subject_id": 515759},
}
# Jimaku 的标题搜索是模糊的：不管搜什么都把这几条全返回，考验全等比对
JIMAKU_ENTRIES = [
    {"id": 729, "name": "Sousou no Frieren", "japanese_name": "葬送のフリーレン",
     "english_name": "Frieren: Beyond Journey’s End", "anilist_id": 154587, "flags": {"anime": True}},
    {"id": 11446, "name": "Sousou no Frieren 2nd Season", "japanese_name": "葬送のフリーレン 第2期",
     "english_name": "Frieren: Beyond Journey’s End Season 2", "anilist_id": 182255,
     "flags": {"anime": True}},
]
ANILIST_YEARS = {154587: 2023, 182255: 2026}


def _fake_bangumi_api(path, params=None):
    if path == "/v0/episodes":
        data = BGM_EPISODES.get(params["subject_id"])
        if data is None:
            return None
        offset = params.get("offset", 0)
        return {"data": data[offset:offset + params.get("limit", 100)], "total": len(data)}
    return BGM_ROUTES.get(path)


def _nfo(path: Path, body: str, root: str = "tvshow"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'<?xml version="1.0" encoding="utf-8"?><{root}>{body}</{root}>', encoding="utf-8")


def test_nfo_supplies_bangumi_id_and_ignores_tvdb_in_id_tag(tmp_path):
    """Jellyfin 写的 tvshow.nfo：<id> 是 TVDB ID，不能当成 tmdb；Bangumi 插件的 ID 在 <bangumiid>。"""
    _make_library(tmp_path, ["葬送的芙莉莲 (2023)"])
    _nfo(tmp_path / "葬送的芙莉莲 (2023)" / "tvshow.nfo",
         "<title>葬送的芙莉莲</title><id>424536</id><tvdbid>424536</tvdbid><bangumiid>400602</bangumiid>")
    settings.media_roots = str(tmp_path)
    e = library.find_shows("芙莉莲")["entries"][0]
    assert e["tmdb_id"] is None and e["bangumi_id"] == 400602 and e["note"] == ""


def test_bangumi_ids_come_from_three_nfo_levels(tmp_path):
    show = tmp_path / "葬送的芙莉莲"
    video = show / "Season 2" / "葬送的芙莉莲 - S02E08 - 第 8 集.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"")
    _nfo(show / "tvshow.nfo", "<bangumiid>400602</bangumiid>")
    _nfo(show / "Season 2" / "season.nfo", '<uniqueid type="Bangumi">515759</uniqueid>', root="season")
    _nfo(video.with_suffix(".nfo"), "<bangumiid>9001</bangumiid>", root="episodedetails")
    ids = library.bangumi_ids_for(video)
    assert (ids.episode, ids.season, ids.show) == (9001, 515759, 400602)
    assert library.bangumi_ids_for(video, show=1).show == 1        # 显式传入的优先于 nfo


def test_locate_walks_to_sequel_for_continuous_numbering():
    """TMDB 把芙莉莲第二季接着第一季排：S01E36 = 第二季第 8 集。"""
    assert bangumi.locate(400602, 36)[:2] == (515759, 8)


def test_locate_understands_bangumi_sort_numbering():
    """咒术回战第二季的条目里，第 25 集就是本季第 1 集；第 5 集就是第 5 集。"""
    assert bangumi.locate(369304, 25)[:2] == (369304, 1)
    assert bangumi.locate(369304, 5)[:2] == (369304, 5)


def test_locate_refuses_when_numbering_is_ambiguous():
    sid, ep, why = bangumi.locate(5000, 10)
    assert sid is None and "交人工" in why


def test_resolve_bangumi_through_mapping_table():
    r = identify.resolve(None, 1, 5, bangumi_ids=bangumi.BangumiIds(show=400602))
    assert (r.anilist_id, r.episode, r.confident, r.source) == (154587, 5, True, "bangumi")


def test_resolve_bangumi_by_exact_title_when_mapping_has_no_links():
    """芙莉莲第二季在映射表里没有外链：靠罗马音别名与 Jimaku 条目名全等。"""
    r = identify.resolve(None, 2, 3, bangumi_ids=bangumi.BangumiIds(season=515759))
    assert (r.anilist_id, r.episode, r.confident) == (182255, 3, True)
    assert "同名" in r.note


def test_resolve_bangumi_continuous_numbering_end_to_end():
    r = identify.resolve(None, 1, 36, bangumi_ids=bangumi.BangumiIds(show=400602))
    assert (r.anilist_id, r.episode) == (182255, 8)


def test_episode_nfo_is_used_only_if_its_number_matches():
    ids = bangumi.BangumiIds(episode=9001, show=400602)
    assert identify.resolve(None, 1, 36, bangumi_ids=ids).episode == 8
    # 集 nfo 说的是第 36 集，视频却是第 5 集：nfo 过期了，不用它，改走剧级 ID
    r = identify.resolve(None, 1, 5, bangumi_ids=ids)
    assert (r.anilist_id, r.episode) == (154587, 5)


def test_show_level_bangumi_id_does_not_cover_later_seasons():
    """剧级 ID 等于第一季；第 2 季没有 season.nfo 时不能拿第一季的字幕去配。"""
    r = identify.resolve(None, 2, 1, bangumi_ids=bangumi.BangumiIds(show=400602))
    assert r.anilist_id is None and "season.nfo" in r.note


def test_title_match_must_be_exact():
    """「葬送」模糊搜得到芙莉莲，但名字不全等，不算。"""
    r = identify.resolve(None, 1, 1, bangumi_ids=bangumi.BangumiIds(show=6000))
    assert r.anilist_id is None and "没有同名条目" in r.note


def test_title_match_checks_air_year(monkeypatch):
    """重制版常常同名：开播年份对不上就不认。"""
    monkeypatch.setattr(identify, "anilist_start_year", lambda aid: 2010)
    r = identify.resolve(None, 2, 3, bangumi_ids=bangumi.BangumiIds(season=515759))
    assert r.anilist_id is None and "年份" in r.note


def test_conflicting_mapping_goes_to_manual():
    """MAL 和 AniDB 指向两个不同的 AniList 条目：数据自相矛盾，不选。"""
    r = identify.resolve(None, 1, 1, bangumi_ids=bangumi.BangumiIds(show=777))
    assert r.anilist_id is None and "多个" in r.note


def test_bangumi_api_down_degrades_quietly(monkeypatch):
    monkeypatch.setattr(bangumi, "_api_get", lambda path, params=None: None)
    r = identify.resolve(None, 1, 5, bangumi_ids=bangumi.BangumiIds(show=400602))
    assert r.anilist_id is None and r.note


def test_scan_reads_bangumi_id_from_nfo(tmp_path, monkeypatch):
    """什么 ID 都不填：从 tvshow.nfo 读到 Bangumi ID，一路配上字幕。"""
    show = tmp_path / "葬送的芙莉莲"
    video = show / "Season 1" / "葬送的芙莉莲 - S01E05 - 第 5 集.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"")
    _nfo(show / "tvshow.nfo", "<bangumiid>400602</bangumiid>")
    monkeypatch.setattr(jimaku, "search_entries", lambda aid: [{"id": 729}] if aid == 154587 else [])
    monkeypatch.setattr(jimaku, "list_files",
                        lambda eid: [_f("[SubsPlease] Sousou no Frieren - 05 (1080p) [ABC]_ja.srt")])
    rep = scan.scan(str(show), dry_run=True)
    ep = rep.episodes[0]
    assert (ep.status, ep.anilist_id, ep.anilist_episode) == ("dry_run", 154587, 5), ep.reason


# ---------- 代理 ----------

class _FakeResponse:
    status_code, content = 200, b"{}"


class _RecordingClient:
    """记下 httpx.Client 收到的参数，不真的发请求。"""
    seen: dict = {}

    def __init__(self, **kwargs):
        _RecordingClient.seen = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, *args, **kwargs):
        return _FakeResponse()


def test_requests_go_through_configured_proxy(monkeypatch):
    """api.bgm.tv 在国内直连不通：外部请求都要走代理（插件里默认跟随 MoviePilot 的设置）。"""
    monkeypatch.setattr(http._httpx, "Client", _RecordingClient)
    settings.proxy = "192.168.1.2:7890"
    http.get_json("https://api.bgm.tv/v0/subjects/1")
    assert _RecordingClient.seen["proxy"] == "http://192.168.1.2:7890"
    settings.proxy = ""
    http.get_json("https://api.bgm.tv/v0/subjects/1")
    assert _RecordingClient.seen["proxy"] is None


def test_proxy_description_hides_credentials():
    settings.proxy = "http://user:secret@10.0.0.2:7890"
    assert http.describe_proxy() == "http://***@10.0.0.2:7890"
    settings.proxy = ""
    assert http.describe_proxy() == "直连"


# ---------- 连不上的站暂时跳过 ----------

class _DeadClient(_RecordingClient):
    calls: list = []

    def request(self, method, url, **kwargs):
        _DeadClient.calls.append(url)
        raise http._httpx.ConnectTimeout("timed out")


def test_unreachable_host_is_skipped_for_a_while(monkeypatch):
    """容器里 api.bgm.tv 连不上时每集都等一次超时，12 集要十几分钟：失败一次后先跳过这个站。"""
    _DeadClient.calls = []
    monkeypatch.setattr(http._httpx, "Client", _DeadClient)
    for _ in range(3):
        with pytest.raises(http.HttpError):
            http.get_json("https://api.bgm.tv/v0/subjects/1")
    assert len(_DeadClient.calls) == 1
    assert "ConnectTimeout" in http.host_problem("api.bgm.tv")
    with pytest.raises(http.HttpError):                 # 别的站不受影响，照常去连
        http.get_json("https://jimaku.cc/api/entries/search")
    assert len(_DeadClient.calls) == 2


def test_network_cause_reaches_the_note(monkeypatch):
    """说明里必须带上连不上的原因，否则用户只看到「未识别」，没法排查。"""
    import time
    http._down["api.bgm.tv"] = (time.time() + 60, "ConnectTimeout（直连）：timed out")
    monkeypatch.setattr(bangumi, "_api_get", lambda path, params=None: None)
    r = identify.resolve(None, 1, 5, bangumi_ids=bangumi.BangumiIds(show=400602))
    assert r.anilist_id is None and "ConnectTimeout" in r.note


# ---------- 识别过程日志 ----------

def test_failed_request_is_logged_with_reason(monkeypatch):
    monkeypatch.setattr(http._httpx, "Client", _DeadClient)
    with trace.capture() as lines:
        for n in (1, 2):
            with pytest.raises(http.HttpError):
                http.get_json(f"https://api.bgm.tv/v0/subjects/{n}")
    assert "失败" in lines[0] and "ConnectTimeout" in lines[0] and "直连" in lines[0]
    assert "跳过" in lines[1]


def test_scan_keeps_a_log_for_each_episode(tmp_path, monkeypatch):
    """「未识别」时页面上要能展开看到每一步：读到哪些 ID、查了什么、为什么没认出来。"""
    show = tmp_path / "葬送的芙莉莲"
    video = show / "Season 1" / "葬送的芙莉莲 - S01E05 - 第 5 集.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"")
    _nfo(show / "tvshow.nfo", "<bangumiid>400602</bangumiid>")
    monkeypatch.setattr(jimaku, "search_entries", lambda aid: [{"id": 729}])
    monkeypatch.setattr(jimaku, "list_files",
                        lambda eid: [_f("[SubsPlease] Sousou no Frieren - 05 (1080p) [ABC]_ja.srt")])
    rep = scan.scan(str(show), dry_run=True)
    log = " | ".join(rep.episodes[0].log)
    assert "Bangumi" in log and "Jimaku" in log and "候选" in log
    assert "tvshow.nfo" in rep.log[0]


def test_process_one_returns_its_log(tmp_path):
    video = tmp_path / "Show S01E05.mkv"
    video.write_bytes(b"")
    out = scan.process_one(None, "", 1, 5, str(video))
    assert out["status"] == "unidentified" and out["log"]
