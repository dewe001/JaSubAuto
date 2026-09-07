"""从 Jimaku 返回的一堆字幕文件里挑出"这一集该用哪个"。

难点在于同一集常有多个版本（不同片源/分辨率/字幕组/格式）。真实样例：
    Sousou no Frieren - 01 「冒険の終わり」 (AT-X 1280x720 x264 AAC).srt
    Sousou no Frieren - 18 「一級魔法使い選抜試験」 (NTV 1920x1080 x264 AAC).ass
    Sousou no Frieren - 18 「一級魔法使い選抜試験」 (NTV 1920x1080 x264 AAC).srt
原则：宁可判定"不确定"交给人工，也不要瞎选（见 CLAUDE.md 硬性约束）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .settings import settings

SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
ARCHIVE_EXTS = {".zip", ".7z", ".rar"}

# 合集/整季包的特征词，这类不做自动匹配
BATCH_KEYWORDS = ("batch", "complete", "全集", "全話", "season", "s01-", "1-12", "1-24")

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
    return any(k in low for k in BATCH_KEYWORDS)


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


def pick(files: list[dict], episode: int) -> tuple[list[Candidate], Candidate | None, str]:
    """筛选并排序候选。

    返回 (候选列表, 自动选中的那个或 None, 说明)。
    自动选中为 None 时表示不确定，必须走人工。
    """
    candidates: list[Candidate] = []
    for f in files:
        name = f.get("name", "")
        ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in ARCHIVE_EXTS:
            continue  # 压缩包需要解压再分发，自动化价值低，跳过
        if ext and ext not in SUBTITLE_EXTS:
            continue
        if is_batch(name):
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

    if len(candidates) == 1:
        return candidates, candidates[0], "唯一匹配，可自动下载"

    top, second = candidates[0], candidates[1]
    if top.score > second.score:
        return candidates, top, f"评分区分出唯一最优（{top.score} > {second.score}，{top.lang_label}）"

    return (
        candidates,
        None,
        f"有 {len(candidates)} 个同分候选（均为{top.lang_label}），无法自动判定，需人工选择"
        f"（在插件配置的「片源偏好」里加上想要的来源即可自动区分）",
    )
