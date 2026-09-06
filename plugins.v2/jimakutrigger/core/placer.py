"""落盘：把选中的字幕按外挂字幕的命名约定放到视频文件旁边。

命名约定：<视频文件basename>.<语言码><原扩展名>，与视频同目录。
    [番剧名] S01E05.mkv
    [番剧名] S01E05.ja.srt
Jellyfin / Emby / Plex 都靠这个约定识别外挂字幕语言，不需要额外配置。
本项目不与任何媒体服务器通信，只按约定写文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import picker
from .settings import settings

# 常见视频容器。用途是拦截"用户给的根本不是视频文件"这类输入，
# 而不是精确识别格式，所以宁可多列几个
VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".flv",
    ".ts", ".m2ts", ".mts", ".rmvb", ".rm", ".webm", ".mpg", ".mpeg", ".iso",
}


@dataclass
class PlaceResult:
    target: Path
    written: bool
    reason: str
    video: Path | None = None      # 实际匹配到的视频文件
    video_note: str = ""           # 怎么匹配到的，便于在界面上核对


def map_path(path: str) -> Path:
    """把 MoviePilot 侧的路径换算成本服务能访问的路径。"""
    if settings.path_mapping_from and path.startswith(settings.path_mapping_from):
        path = settings.path_mapping_to + path[len(settings.path_mapping_from):]
    return Path(path.replace("\\", "/"))


def existing_ja_subtitle(video: Path) -> Path | None:
    """视频旁边是否已经有日语字幕，有就返回那个文件。

    只比对扩展名会漏：已存在 X.ja.ass 时，若这次准备写 X.ja.srt，`target.exists()`
    是 False，结果又加了一份重复字幕。所以这里按「同名 + .ja. 前缀」找任意字幕扩展名。
    """
    prefix = f"{video.stem}.{settings.subtitle_lang_suffix}.".casefold()
    parent = video.parent
    if not parent.is_dir():
        return None
    for f in parent.iterdir():
        if (f.is_file() and f.name.casefold().startswith(prefix)
                and f.suffix.lower() in picker.SUBTITLE_EXTS):
            return f
    return None


def resolve_video(video_path: str, library_episode: int | None = None) -> tuple[Path | None, str]:
    """把用户/插件给的路径解析成一个具体的视频文件。

    支持两种输入：
      1. 直接给视频文件路径 —— 插件走的就是这条
      2. 给视频所在的文件夹 + 媒体库集号 —— 手动模式下更顺手，由本函数按集号找出对应视频

    找不到或不唯一一律返回 None，绝不猜（见 CLAUDE.md 硬性约束）。
    注意 `library_episode` 必须是**媒体库口径**的集号（即 TMDB 集号，文件名里写的那个），
    不是换算到 AniList 之后的集号——两者在分季错位的番里不是一回事。
    """
    p = map_path(video_path)

    if p.is_dir():
        if library_episode is None:
            return None, f"给的是文件夹（{p}），但没提供媒体库集号，无法确定对应哪个视频文件"
        videos = [f for f in sorted(p.iterdir())
                  if f.is_file() and f.suffix.lower() in VIDEO_EXTS]
        if not videos:
            return None, f"文件夹里没有视频文件：{p}"
        matches = [f for f in videos if picker.parse_episode(f.name) == library_episode]
        if not matches:
            listed = "、".join(f.name for f in videos[:5])
            return None, (f"文件夹里没有第 {library_episode} 集的视频（共 {len(videos)} 个视频文件，"
                          f"例如：{listed}）")
        if len(matches) > 1:
            listed = "、".join(f.name for f in matches)
            return None, f"文件夹里有 {len(matches)} 个文件都像第 {library_episode} 集，无法判定：{listed}"
        return matches[0], f"按第 {library_episode} 集在文件夹里匹配到 {matches[0].name}"

    if p.suffix.lower() not in VIDEO_EXTS:
        return None, (f"这不像视频文件路径：{p}。"
                      f"请填到具体的视频文件（如 .mkv），或填视频所在文件夹并给出媒体库集号")

    return p, ""


def target_path(video_path: str | Path, subtitle_name: str) -> Path:
    """算出字幕应该放在哪、叫什么。"""
    video = Path(video_path) if isinstance(video_path, str) else video_path
    ext = "." + subtitle_name.rsplit(".", 1)[-1].lower() if "." in subtitle_name else ".srt"
    return video.with_name(f"{video.stem}.{settings.subtitle_lang_suffix}{ext}")


def place(video_path: str, subtitle_name: str, content: bytes | None,
          dry_run: bool | None = None, library_episode: int | None = None) -> PlaceResult:
    """写入字幕文件。

    三条安全阀（见 CLAUDE.md）：
      1. dry_run 默认取配置值，配置默认为 true
      2. 绝不覆盖已存在的字幕文件
      3. 目标目录不存在就报错，不自作主张创建
      4. 解析不出唯一的视频文件就拒绝，不拿目录名/猜出来的名字凑合
    """
    dry_run = settings.dry_run if dry_run is None else dry_run
    if not video_path or not video_path.strip():
        return PlaceResult(Path("."), False, "未提供视频文件路径，无法确定字幕位置")

    video, note = resolve_video(video_path, library_episode)
    if video is None:
        return PlaceResult(Path("."), False, note)
    if not video.name:
        return PlaceResult(video, False, f"视频路径不合法：{video_path!r}")
    target = target_path(video, subtitle_name)

    existing = existing_ja_subtitle(video)
    if existing:
        return PlaceResult(target, False, f"已有日语字幕 {existing.name}，跳过（绝不覆盖）", video, note)
    if dry_run:
        return PlaceResult(target, False, "dry-run：仅打印，未写盘", video, note)
    if not video.exists():
        return PlaceResult(target, False, f"视频文件不存在，路径映射可能不对：{video}", video, note)
    if content is None:
        return PlaceResult(target, False, "没有字幕内容可写", video, note)

    target.write_bytes(content)
    return PlaceResult(target, True, "已写入", video, note)
