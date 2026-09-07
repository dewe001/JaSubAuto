"""把日语字幕和视频旁边已有的中文字幕合成一份中日双语字幕。

为什么需要：Jimaku 上的双语字幕（Nekomoe kissaten 的 JPSC 之类）只有一部分番有，
大多数条目是单语的。而中文字幕 MoviePilot 已经下好放在视频旁边了，两边一合就是双语。

中文只认**视频旁边的外置中文字幕文件**（MoviePilot 下的那份）。Jimaku 是日语字幕站，
没有中文字幕可下；内嵌在视频里的和烧进画面的都不处理——抽内嵌轨要拖一个 ffmpeg 外部命令
进来，插件形式跑在 MoviePilot 容器里没法保证它存在，不值得为此加一条不可靠的依赖。

做法：以**日语字幕的时间轴为准**（它是这次下载的、和这个片源对得上的那份），
逐条去中文字幕里找时间上重叠最多的一条，把中文接在日语下面。

原则同样是"对不上就别硬合"：
  * 匹配率低于 MIN_MATCH_RATE 判定为两边时间轴不是一个片源，放弃合并，写纯日语
  * 解析不出、编码认不出、格式不认识，一律返回 None 让调用方走原路
  * 只读中文字幕，不动它
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from pathlib import Path

from .cleaner import decode as _decode

# 低于这个匹配率就认为两份字幕不是同一个片源，不合并
MIN_MATCH_RATE = 0.5
# 重叠时长至少要占较短那条的这个比例，才算同一句话
MIN_OVERLAP_RATIO = 0.3

SUBTITLE_EXTS = (".srt", ".ass", ".ssa", ".vtt")

# 中文字幕的语言标记。MoviePilot / 字幕组的写法都覆盖到
_ZH_TOKENS = {
    "zh", "chs", "cht", "chi", "chn", "cn", "sc", "tc", "gb", "gbk", "big5",
    "hans", "hant", "zhhans", "zhhant", "zhcn", "zhtw", "zhhk",
    "中文", "简体", "繁体", "繁體", "简中", "繁中", "简", "繁",
}
# 简体优先：学日语时对照用，简繁都行，但同时存在就挑简体
_SIMPLIFIED_HINTS = ("hans", "chs", "sc", "gb", "cn", "简")

_SRT_TIME = re.compile(
    r"(\d{1,3}):([0-5]?\d):([0-5]?\d)[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,3}):([0-5]?\d):([0-5]?\d)[,.](\d{1,3})"
)
_ASS_TIME = re.compile(r"^(\d{1,3}):([0-5]?\d):([0-5]?\d)[.:](\d{1,2})$")
_ASS_TAG = re.compile(r"\{[^}]*\}")
_ASS_DRAWING = re.compile(r"\\p[1-9]")
# 明显不是台词的 ASS 样式/角色名，合并时跳过，免得把水印和staff表接到台词下面
_NON_DIALOGUE = ("sign", "staff", "logo", "credit", "title", "水印", "注释", "屏幕", "字幕组")

_BLOCK_SPLIT = re.compile(r"\r?\n[\s　]*\r?\n")


@dataclass
class Cue:
    start: int          # 毫秒
    end: int
    text: str           # 可能含换行


# ---------- 解析 ----------

def _ms(h: str, m: str, s: str, frac: str, digits: int) -> int:
    scale = 10 ** (3 - digits)
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(frac) * scale


def parse_srt(text: str) -> list[Cue]:
    """.srt / .vtt：按空行分块，含 --> 的那行是时间轴。"""
    cues: list[Cue] = []
    for block in _BLOCK_SPLIT.split(text):
        lines = block.splitlines()
        idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if idx is None:
            continue
        m = _SRT_TIME.search(lines[idx])
        if not m:
            continue
        g = m.groups()
        start = _ms(g[0], g[1], g[2], g[3], len(g[3]))
        end = _ms(g[4], g[5], g[6], g[7], len(g[7]))
        body = "\n".join(l for l in lines[idx + 1:] if l.strip())
        if body.strip():
            cues.append(Cue(start, end, body.strip()))
    return cues


def parse_ass(text: str) -> list[Cue]:
    r""".ass / .ssa：只取 Dialogue 行，去掉 {\...} 特效标签，\N 还原成换行。"""
    cues: list[Cue] = []
    for line in text.splitlines():
        if not line.lower().startswith("dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) < 10:
            continue
        raw = parts[9]
        if _ASS_DRAWING.search(raw):        # 矢量绘图，不是台词
            continue
        style_name = (parts[3] + " " + parts[4]).lower()
        if any(k in style_name for k in _NON_DIALOGUE):
            continue
        t0 = _ASS_TIME.match(parts[1].strip())
        t1 = _ASS_TIME.match(parts[2].strip())
        if not t0 or not t1:
            continue
        body = _ASS_TAG.sub("", raw).replace("\\N", "\n").replace("\\n", "\n")
        body = body.replace("\\h", " ").strip()
        if not body:
            continue
        cues.append(Cue(_ms(*t0.groups(), len(t0.group(4))),
                        _ms(*t1.groups(), len(t1.group(4))), body))
    return cues


def parse(content: bytes, filename: str) -> list[Cue] | None:
    """按扩展名解析成 Cue 列表。认不出格式或解析为空返回 None。"""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext not in SUBTITLE_EXTS:
        return None
    text = _decode(content)
    if text is None:
        return None
    try:
        cues = parse_ass(text) if ext in (".ass", ".ssa") else parse_srt(text)
    except Exception:                        # 失败必须静默降级
        return None
    return cues or None


# ---------- 渲染 ----------

def _stamp(ms: int) -> str:
    ms = max(ms, 0)
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def render_srt(cues: list[Cue]) -> str:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{_stamp(c.start)} --> {_stamp(c.end)}\n{c.text}")
    return "\n\n".join(out) + "\n"


# ---------- 合并 ----------

def merge_cues(ja: list[Cue], zh: list[Cue]) -> tuple[list[Cue], int]:
    """以日语时间轴为准，逐条贴上重叠最多的中文。返回 (合并结果, 匹配上的条数)。"""
    zh_sorted = sorted(zh, key=lambda c: c.start)
    starts = [c.start for c in zh_sorted]

    merged: list[Cue] = []
    matched = 0
    for c in ja:
        # 只看开始时间落在 [c.start - 最长中文时长, c.end] 附近的那几条，别每条都全表扫
        lo = max(bisect.bisect_left(starts, c.start) - 8, 0)
        hi = min(bisect.bisect_right(starts, c.end) + 8, len(zh_sorted))
        best, best_overlap = None, 0
        for z in zh_sorted[lo:hi]:
            overlap = min(c.end, z.end) - max(c.start, z.start)
            if overlap <= 0:
                continue
            shorter = max(min(c.end - c.start, z.end - z.start), 1)
            if overlap < shorter * MIN_OVERLAP_RATIO:
                continue
            if overlap > best_overlap:
                best, best_overlap = z, overlap
        if best is None:
            merged.append(c)
            continue
        matched += 1
        # 日语在上、中文在下：先读日语，读不懂再往下瞄一眼
        merged.append(Cue(c.start, c.end, f"{c.text}\n{best.text}"))
    return merged, matched


# ---------- 找视频旁边的中文字幕 ----------

def _lang_part(video_stem: str, name: str) -> str:
    """取 <视频名>.<语言标记>.<扩展名> 中间那段语言标记，取不到返回空串。"""
    if not name.lower().startswith(video_stem.lower() + "."):
        return ""
    rest = name[len(video_stem) + 1:]
    if "." not in rest:
        return ""
    return rest.rsplit(".", 1)[0].lower()


def _is_chinese_tag(tag: str) -> bool:
    if not tag:
        return False
    parts = {p for p in re.split(r"[^0-9a-z一-鿿]+", tag) if p}
    parts.add(tag.replace("-", "").replace("_", ""))
    return bool(parts & _ZH_TOKENS)


def find_chinese_subtitle(video: Path, ja_suffix: str = "ja") -> Path | None:
    """找视频旁边的中文字幕。找不到或分不清返回 None，绝不拿无语言标记的文件顶替。"""
    parent = video.parent
    if not parent.is_dir():
        return None
    hits: list[Path] = []
    for f in sorted(parent.iterdir()):
        if not f.is_file() or f.suffix.lower() not in SUBTITLE_EXTS:
            continue
        tag = _lang_part(video.stem, f.name)
        if not tag or tag.startswith(ja_suffix.lower()):
            continue
        if _is_chinese_tag(tag):
            hits.append(f)
    if not hits:
        return None
    hits.sort(key=lambda f: 0 if any(h in f.name.lower() for h in _SIMPLIFIED_HINTS) else 1)
    return hits[0]


def chinese_source(video: Path, ja_suffix: str = "ja") -> tuple[bytes | None, str, str]:
    """取视频旁边的中文字幕，返回 (内容, 文件名, 失败说明)。没有就返回 (None, "", 原因)。"""
    zh_file = find_chinese_subtitle(video, ja_suffix)
    if zh_file is None:
        return None, "", "旁边没有中文字幕"
    try:
        return zh_file.read_bytes(), zh_file.name, ""
    except OSError as exc:
        return None, "", f"读不了中文字幕 {zh_file.name}：{exc}"


def merge_with_chinese(video: Path, ja_content: bytes, ja_name: str,
                       ja_suffix: str = "ja") -> tuple[bytes | None, str]:
    """把日语字幕和找到的中文字幕合成双语。

    返回 (合并后的 srt 字节 或 None, 说明)。返回 None 表示这次不合并，
    调用方照常写纯日语字幕——任何一步不确定都走这条路。
    """
    zh_content, zh_name, why = chinese_source(video, ja_suffix)
    if zh_content is None:
        return None, why + "，写纯日语"

    ja_cues = parse(ja_content, ja_name)
    if not ja_cues:
        return None, f"日语字幕解析不出内容（{ja_name}），不合并"
    zh_cues = parse(zh_content, zh_name)
    if not zh_cues:
        return None, f"中文字幕解析不出内容（{zh_name}），不合并"

    merged, matched = merge_cues(ja_cues, zh_cues)
    rate = matched / len(ja_cues)
    if rate < MIN_MATCH_RATE:
        return None, (f"与 {zh_name} 时间轴对不上（仅 {matched}/{len(ja_cues)} 条重叠，"
                      f"{rate:.0%}），多半不是同一个片源，只写纯日语")
    return (render_srt(merged).encode("utf-8"),
            f"已与 {zh_name} 合成中日双语（{matched}/{len(ja_cues)} 条配上中文）")
