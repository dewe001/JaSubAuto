"""写盘前清洗字幕文本：去掉说话人标注和音效描述。

日语听障字幕（CC/SDH）逐字听写、时间轴最准，但每行前面挂着说话人名：

    （フリーレン）そうだね
    （ドアが開く音）
    ♪〜

实测同一集的五个不同来源都带这类标注（不只是文件名标了 [cc] 的那些），
所以清洗与语言分类无关，对所有下载下来的字幕统一做。

原则是"改不动就别改"：
  * 只删**行首**的整组标注和**整行只有标注**的行，行中间的括号一律不碰
    （`そう（笑）` 这种是台词的一部分）
  * 一条字幕的所有文本行都被删光时，整条丢掉并重新编号
  * 没有删掉任何东西时**原样返回输入的字节**——因此即使编码猜错也不会写出乱码
  * 不认识的格式（.sub 等）直接不处理
"""

from __future__ import annotations

import re

# 行首的说话人标注：（アイゼン）【ナレーション】。半角括号只在里面有中日文字时才算，
# 免得把英文歌词里的 (x2) 之类当标注
_LEADING = re.compile(
    r"^[\s　]*(?:（[^（）]*）|【[^【】]*】|＜[^＜＞]*＞|\([^()]*[぀-ヿ一-鿿][^()]*\))[\s　]*"
)
# ASS 的花括号特效标签，清洗时要原样留在行首
_ASS_TAGS = re.compile(r"^(?:\{[^}]*\}[\s　]*)*")
# 整行只有音符（无歌词的配乐提示）
_MUSIC_ONLY = re.compile(r"^[\s　♪♬♩〜～~]+$")

_ENCODINGS = ("utf-8-sig", "cp932", "gb18030", "utf-16")


def _clean_line(text: str) -> tuple[str, int]:
    """清洗一条文本行，返回 (结果, 删掉的标注数)。

    没删到东西就原样返回，调用方据此判断"这一行不要动"——
    避免把只有特效标签、本来就是空的行误删。
    """
    m = _ASS_TAGS.match(text)
    prefix, body = (m.group(0), text[m.end():]) if m else ("", text)

    removed = 0
    while True:
        hit = _LEADING.match(body)
        if not hit:
            break
        body = body[hit.end():]
        removed += 1

    if body.strip() and _MUSIC_ONLY.match(body):
        body, removed = "", removed + 1

    if removed == 0:
        return text, 0
    if not body.strip():
        return "", removed
    return prefix + body, removed


def _clean_segments(segments: list[str]) -> tuple[list[str], int]:
    kept, removed = [], 0
    for seg in segments:
        cleaned, n = _clean_line(seg)
        removed += n
        # n == 0 表示这行没被碰过，原样保留（哪怕它本来就是空的）
        if n == 0 or cleaned.strip():
            kept.append(cleaned)
    return kept, removed


_BLOCK_SPLIT = re.compile(r"\r?\n[\s　]*\r?\n")


def _clean_srt(text: str) -> tuple[str, int]:
    """.srt / .vtt：按空行分块，块内第一条含 --> 的是时间轴，之后才是文本。"""
    blocks, removed, index = [], 0, 0
    for block in _BLOCK_SPLIT.split(text):
        lines = block.splitlines()
        timing = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if timing is None:                      # WEBVTT 头、样式块之类，原样留着
            if block.strip():
                blocks.append(block)
            continue
        head, body = lines[:timing + 1], lines[timing + 1:]
        kept, n = _clean_segments(body)
        removed += n
        if not any(l.strip() for l in kept):    # 整条都是标注，丢掉
            continue
        index += 1
        if head and head[0].strip().isdigit():  # 丢过块之后序号要连续
            head = [str(index)] + head[1:]
        blocks.append("\n".join(head + kept))
    return "\n\n".join(blocks) + "\n", removed


def _clean_ass(text: str) -> tuple[str, int]:
    r""".ass / .ssa：只动 Dialogue 行的第 10 个字段（Text），\N 是行内换行。"""
    out, removed = [], 0
    for line in text.splitlines():
        if not line.lower().startswith("dialogue:"):
            out.append(line)
            continue
        parts = line.split(",", 9)
        if len(parts) < 10:                     # 字段不全，不认识就别动
            out.append(line)
            continue
        kept, n = _clean_segments(parts[9].split(r"\N"))
        removed += n
        if not any(s.strip() for s in kept):
            continue
        parts[9] = r"\N".join(kept)
        out.append(",".join(parts))
    return "\n".join(out) + "\n", removed


def decode(data: bytes) -> str | None:
    """按常见字幕编码依次尝试。全都失败返回 None（调用方据此放弃处理）。"""
    for enc in _ENCODINGS:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def clean(content: bytes, filename: str) -> tuple[bytes, int]:
    """返回 (清洗后的字节, 删掉的标注数)。任何一步不确定都原样返回，绝不劣化。"""
    if not content:
        return content, 0
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext in (".ass", ".ssa"):
        handler = _clean_ass
    elif ext in (".srt", ".vtt"):
        handler = _clean_srt
    else:
        return content, 0

    text = decode(content)
    if text is None:
        return content, 0
    try:
        cleaned, removed = handler(text)
    except Exception:                            # 失败必须静默降级
        return content, 0
    if removed == 0:
        return content, 0                        # 没删到东西就别重写编码
    return cleaned.encode("utf-8"), removed
