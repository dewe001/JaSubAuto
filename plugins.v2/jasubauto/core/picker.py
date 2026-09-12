"""从 Jimaku 返回的一堆字幕文件里挑出"这一集该用哪个"。

难点在于同一集常有多个版本（不同片源/分辨率/字幕组/格式）。真实样例：
    Sousou no Frieren - 01 「冒険の終わり」 (AT-X 1280x720 x264 AAC).srt
    Sousou no Frieren - 18 「一級魔法使い選抜試験」 (NTV 1920x1080 x264 AAC).ass
    Sousou no Frieren - 18 「一級魔法使い選抜試験」 (NTV 1920x1080 x264 AAC).srt
原则（见 CLAUDE.md 硬性约束）：认不准是哪一集就不选；确定是这一集、只是版本不同，
就按固定规则选，不交人工——同分交人工曾让整季 12 集挨个手点。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .settings import settings

SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
ARCHIVE_EXTS = {".zip", ".7z", ".rar"}

# 合集/整季包的特征词，这类不做自动匹配。
# 不能放 "season"：「Show 2nd Season - 01.srt」是单集，放了会被当成合集丢掉
BATCH_KEYWORDS = ("batch", "complete", "全集", "全話", "s01-")
# 集号范围「- 01-04 TVSP」「[01-12]」：两个数紧贴连字符，后一个大于前一个。
# 前面不许是连字符或数字，避免把日期 2024-06-18 里的「06-18」认成范围
_RANGE = re.compile(r"(?<![\d.\-])(\d{1,3})[-~～](\d{1,3})(?!\d)")

# 同一份字幕常同时发 ass 和 srt，内容一样只是格式不同，按这个顺序取。
# srt 排第一：所有播放端都能直接显示；ass 遇到不支持的客户端会让 Jellyfin 转码烧录，白占 NAS 性能
FORMAT_ORDER = (".srt", ".ass", ".ssa", ".vtt", ".sub")

_GROUP_BRACKET = re.compile(r"^\s*\[([^\]]+)\]")          # [Nekomoe kissaten&LoliHouse] Show - 01 ...
_GROUP_SCENE = re.compile(r"-([A-Za-z][A-Za-z0-9]*)$")    # Show.S01E01.720p.WEB-DL.H.264-ToonsHub

# 匹配集号前先剔除的噪音：分辨率、编码、音频、年份、位深
_NOISE = re.compile(
    r"\d{3,4}\s*[xX]\s*\d{3,4}"      # 1280x720
    r"|\b[xXhH]\.?26[45]\b"           # x264 / H.265
    r"|\b\d{3,4}[pi]\b"               # 1080p / 720i
    r"|\b(?:10|8)\s*bits?\b"
    r"|\b(?:AAC|FLAC|OPUS|DTS|AC3|EAC3)\b"
    r"|\((?:19|20)\d{2}\)"            # (2023)
    r"|\b(?:19|20)\d{2}\b",
    re.IGNORECASE,
)

# 集号模式，按可信度从高到低
_EPISODE_PATTERNS = (
    re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})"),            # S01E05 -> 取第二组
    re.compile(r"\b[Ee][Pp]?(?:isode)?[ ._-]*(\d{1,3})\b"),  # E05 / Ep 05 / Episode 05
    re.compile(r"第\s*(\d{1,3})\s*[話话集]"),               # 第05話
    re.compile(r"\[(\d{1,3})\]"),                          # [01]，字幕组常用；纯数字才算，CRC 如 [8E3F8FA5] 不会误命中
    re.compile(r"[-–—]\s*(\d{1,3})(?:\s|$|[^\dxX])"),      # " - 05 "，这批文件最常见
    re.compile(r"#(\d{1,3})\b"),                           # #05
    re.compile(r"(?:^|[\s._])(\d{1,3})(?:[\s._]|$)"),      # 兜底：孤立数字
)


# ---------- 语言纯度 ----------
# 用途是学日语，日英/纯外语基本没用，所以语言构成是主导项。
# 两种偏好（配置项「字幕偏好」）：
#   * 中日双语优先（默认）：遇到生词不用停下来查，学习效率高
#   * 纯日语优先：不想看到中文时用
# Jimaku 是日语字幕站，没有任何语言标记的文件绝大多数就是纯日语，因此给次高分。
# 说话人标注（CC/SDH）已经由 cleaner.py 在写盘前清掉，但它仍排在干净台词之后——
# 清洗只能删标注，删不掉听写体带来的其它差异。
LANG_SCORES = {
    "ja": 300,        # 纯日语、纯台词
    "unknown": 280,   # 无语言标记，在 Jimaku 上大概率是纯日语；电视/BD 源一般也没有听障注释
    "ja_cc": 260,     # 纯日语但含 CC/SDH 听障注释（说话人名、音效描述），学日语时是干扰
    "ja_zh": 200,     # 日中双语
    "ja_other": 60,   # 日英等其它双语
    "non_ja": 20,     # 压根没有日语
}
# 中日双语优先（默认）：日语台词旁边就有中文，遇到生词不用停下来查
LANG_SCORES_BILINGUAL = {**LANG_SCORES, "ja_zh": 320}

LANG_LABELS = {
    "ja": "纯日语",
    "unknown": "无语言标记（大概率纯日语）",
    "ja_cc": "纯日语·含听障注释",
    "ja_zh": "日中双语",
    "ja_other": "日+其它语言双语",
    "non_ja": "不含日语",
}

def lang_scores() -> dict:
    """按配置的字幕偏好取一张打分表。两张表只差 ja_zh 一项的位置。"""
    return LANG_SCORES_BILINGUAL if settings.subtitle_pref != "japanese" else LANG_SCORES


_ZH_JA_SUBSTR = ("jpsc", "jptc", "scjp", "tcjp", "中日", "日中", "简日", "繁日", "日简", "日繁")
_ZH_TOKENS = {"sc", "tc", "chs", "cht", "gb", "big5", "zh", "chi", "chn", "cn", "简体", "繁体", "繁體", "中文"}
_EN_SUBSTR = ("ja-en", "jaen", "jp-en", "jpen", "ja_en")
_EN_TOKENS = {"en", "eng", "english"}
_JA_SUBSTR = ("ja-jp", "ja_jp", "日本語")
_JA_TOKENS = {"ja", "jp", "jpn", "japanese", "jap"}
# CC/SDH：官方流媒体的听障字幕，逐字听写但夹带 （アイゼン） 这类说话人标注和音效描述。
# 只认方括号形式和独立词，避免 "Accel"、"Succession" 里的 cc 被误判
_SDH_TOKENS = {"sdh", "cc"}


def classify_language(name: str) -> tuple[str, str]:
    """判断字幕文件的语言构成，返回 (类别, 说明)。"""
    low = name.lower()
    tokens = set(re.split(r"[^0-9a-z一-鿿぀-ヿ]+", low)) - {""}

    if any(s in low for s in _ZH_JA_SUBSTR):
        return "ja_zh", LANG_LABELS["ja_zh"]

    has_ja = bool(_JA_TOKENS & tokens) or any(s in low for s in _JA_SUBSTR)
    has_zh = bool(_ZH_TOKENS & tokens)
    has_en = bool(_EN_TOKENS & tokens) or any(s in low for s in _EN_SUBSTR)

    if has_ja and has_zh:
        return "ja_zh", LANG_LABELS["ja_zh"]
    if has_ja and has_en:
        return "ja_other", LANG_LABELS["ja_other"]
    if has_ja:
        if _SDH_TOKENS & tokens:
            return "ja_cc", LANG_LABELS["ja_cc"]
        return "ja", LANG_LABELS["ja"]
    if has_zh or has_en:
        return "non_ja", LANG_LABELS["non_ja"]
    return "unknown", LANG_LABELS["unknown"]


@dataclass
class Candidate:
    name: str
    url: str
    size: int
    last_modified: str
    episode: int | None
    lang: str = "unknown"
    lang_label: str = ""
    score: int = 0
    reasons: list[str] | None = None


def parse_episode(filename: str) -> int | None:
    """从字幕文件名里解析集号，解析不出返回 None。"""
    cleaned = _NOISE.sub(" ", filename)
    # 去掉扩展名，避免 ".ass" 之类干扰
    cleaned = re.sub(r"\.[A-Za-z0-9]{2,4}$", " ", cleaned)
    for i, pattern in enumerate(_EPISODE_PATTERNS):
        m = pattern.search(cleaned)
        if m:
            # S01E05 模式取第二个捕获组，其余取第一个
            value = m.group(2) if i == 0 else m.group(1)
            try:
                return int(value)
            except ValueError:
                continue
    return None


def is_batch(filename: str) -> bool:
    low = filename.lower()
    if any(k in low for k in BATCH_KEYWORDS):
        return True
    # 分辨率、年份这类数字先剔掉，再找集号范围
    return any(int(b) > int(a) for a, b in _RANGE.findall(_NOISE.sub(" ", filename)))


def _ext(name: str) -> str:
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _stem(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


def _usable(name: str) -> bool:
    """能拿来逐集自动匹配的字幕文件：是字幕、不是压缩包、不是合集。"""
    ext = _ext(name)
    if ext in ARCHIVE_EXTS:
        return False  # 压缩包需要解压再分发，自动化价值低，跳过
    if ext and ext not in SUBTITLE_EXTS:
        return False
    return not is_batch(name)


def _format_rank(name: str) -> int:
    ext = _ext(name)
    return FORMAT_ORDER.index(ext) if ext in FORMAT_ORDER else len(FORMAT_ORDER)


def named_group(name: str) -> str:
    """文件名里写明的发布组：开头的 [组名]，或 scene 命名末尾的 -组名。没写返回空串。"""
    stem = _stem(name)
    m = _GROUP_BRACKET.match(stem)
    if m:
        return m.group(1).strip()
    m = _GROUP_SCENE.search(stem)
    if m and m.group(1).lower() not in _JA_TOKENS | _EN_TOKENS | _ZH_TOKENS:
        return m.group(1)
    return ""


def release_group(name: str) -> str:
    """这个文件属于哪条发布线（小写）。同一条线的各集返回同一个值。

    没写组名的，去掉分集标题和所有数字后剩下的部分就是它的"线"：
    「Kinomi Master - 01 「唯一の素材」 (MX 1920x1080 x264 AAC)」各集归到同一个键。
    """
    group = named_group(name)
    if group:
        return group.lower()
    rest = re.sub(r"「[^」]*」|\d+", " ", _stem(name).lower())
    return " ".join(t for t in re.split(r"[^a-z぀-ヿ一-鿿]+", rest) if t)


def _group_in(group: str, hint: str) -> bool:
    """发布组是否出现在视频文件名里。按整词比，避免 "web" 命中 "WebRip"。
    合作组「Nekomoe kissaten&LoliHouse」拆开后任何一个组对上就算。"""
    parts = {p.strip() for p in re.split(r"[&+,/]", group.lower()) if len(p.strip()) >= 3}
    return any(re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", hint) for p in parts)


def _score(name: str, lang: str, lang_label: str) -> tuple[int, list[str]]:
    """打分 = 语言纯度（主）+ 片源偏好关键词（次）。

    语言纯度是主导项：学日语的场景下，一个纯日语的冷门源也好过日英双语的热门源。
    """
    score = lang_scores().get(lang, 0)
    reasons = [f"{lang_label} +{score}"]
    keywords = settings.preferred_keywords
    for rank, kw in enumerate(keywords):
        if kw.lower() in name.lower():
            # 上限 19：语言分档最小间距是 20，片源偏好再怎么加也不能把低一档的顶上来。
            # 只算优先级最高的那个命中，多个关键词命中同一个文件时不叠加。
            gain = min(len(keywords) - rank, 19)
            score += gain
            reasons.append(f"片源偏好「{kw}」+{gain}")
            break
    return score, reasons


def _group_stats(files: list[dict], lang: str) -> dict[str, tuple[int, str]]:
    """同一语言档里，每条发布线覆盖了几集、最近一次更新是什么时候。"""
    episodes: dict[str, set[int]] = {}
    latest: dict[str, str] = {}
    for f in files:
        name = f.get("name", "")
        if not _usable(name) or classify_language(name)[0] != lang:
            continue
        ep = parse_episode(name)
        if ep is None:
            continue
        group = release_group(name)
        episodes.setdefault(group, set()).add(ep)
        latest[group] = max(latest.get(group, ""), f.get("last_modified") or "")
    return {g: (len(eps), latest[g]) for g, eps in episodes.items()}


def _break_tie(tied: list[Candidate], files: list[dict], hint: str) -> tuple[Candidate, str]:
    """同分候选里挑一个，返回 (选中的, 按哪条规则选的)。

    它们都是这一集、同一语言档的字幕，挑哪个都不算下错。要紧的是**整部剧各集挑到同一个组**，
    否则字幕风格一集一变——所以除了第一步，规则都按组算（覆盖集数、组内最近更新），
    这些指标每一集算出来都一样，各集自然落在同一组。
    """
    # 1. 同一份字幕的不同格式只留一个
    by_release: dict[str, Candidate] = {}
    for c in tied:
        key = _stem(c.name).lower()
        if key not in by_release or _format_rank(c.name) < _format_rank(by_release[key].name):
            by_release[key] = c
    pool = list(by_release.values())
    if len(pool) == 1:
        return pool[0], f"是同一份字幕的不同格式，取 {_ext(pool[0].name)}"

    # 2. 和视频出自同一个发布组的，时间轴最可能对得上
    hint = (hint or "").lower()
    if hint:
        same = [c for c in pool if named_group(c.name) and _group_in(named_group(c.name), hint)]
        if len(same) == 1:
            return same[0], f"与视频同为「{named_group(same[0].name)}」发布"
        if same:
            pool = same

    # 3~5. 按组比：覆盖集数多 → 组内最近更新 → 组名排序
    stats = _group_stats(files, pool[0].lang)
    pool.sort(key=lambda c: (release_group(c.name), _format_rank(c.name), c.name))
    pool.sort(key=lambda c: stats.get(release_group(c.name), (0, "")), reverse=True)
    chosen = pool[0]
    group = release_group(chosen.name)
    label = named_group(chosen.name) or group
    rival = next((c for c in pool[1:] if release_group(c.name) != group), None)
    if rival is None:
        return chosen, f"都出自「{label}」，按格式和文件名取第一个"

    eps, latest = stats.get(group, (0, ""))
    rival_eps, rival_latest = stats.get(release_group(rival.name), (0, ""))
    if eps > rival_eps:
        how = f"「{label}」覆盖 {eps} 集，比其它组多"
    elif latest > rival_latest:
        how = f"各组都有 {eps} 集，取最近更新的「{label}」（{latest[:10]}）"
    else:
        how = f"各组集数和更新时间都相同，按组名取「{label}」"
    return chosen, how + "，整部剧都用这一组"


def pick(files: list[dict], episode: int, hint: str = "") -> tuple[list[Candidate], Candidate | None, str]:
    """筛选并排序候选。

    返回 (候选列表, 选中的那个或 None, 说明)。只有一个候选都没有时才返回 None。
    同分不交人工，由 `_break_tie` 按规则挑。`hint` 是视频文件名（自动模式还会带上
    下载时的原始文件名），用来找同一个发布组的字幕。
    """
    candidates: list[Candidate] = []
    for f in files:
        name = f.get("name", "")
        if not _usable(name):
            continue
        ep = parse_episode(name)
        if ep != episode:
            continue
        lang, lang_label = classify_language(name)
        score, reasons = _score(name, lang, lang_label)
        candidates.append(
            Candidate(
                name=name,
                url=f.get("url", ""),
                size=f.get("size", 0),
                last_modified=f.get("last_modified", ""),
                episode=ep,
                lang=lang,
                lang_label=lang_label,
                score=score,
                reasons=reasons,
            )
        )

    if not candidates:
        return [], None, f"没有匹配第 {episode} 集的字幕文件"

    # 排序：偏好分 > 修改时间新 > 体积大
    candidates.sort(key=lambda c: (c.score, c.last_modified, c.size), reverse=True)
    top = candidates[0]
    tied = [c for c in candidates if c.score == top.score]
    if len(tied) == 1:
        if len(candidates) == 1:
            return candidates, top, "唯一匹配，可自动下载"
        return candidates, top, f"评分区分出唯一最优（{top.score} > {candidates[1].score}，{top.lang_label}）"

    chosen, how = _break_tie(tied, files, hint)
    # 选中的排最前，和页面上的「推荐」一致
    candidates = [chosen] + [c for c in candidates if c is not chosen]
    return candidates, chosen, f"{len(tied)} 个候选同为{top.lang_label}：{how}"
