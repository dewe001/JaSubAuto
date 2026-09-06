"""M0 命令行工具：跑通并肉眼验证完整链路。

用法：
    python -m service.cli --tmdb-id 209867 --season 1 --episode 5
    python -m service.cli --tmdb-id 209867 --season 1 --episode 29      # 跨季偏移的例子
    python -m service.cli --anilist-id 154587 --episode 5               # 跳过识别，直接查
    python -m service.cli --tmdb-id 209867 --season 1 --episode 5 \
        --video "D:/media/anime/Frieren/Season 1/Frieren - S01E05.mkv" --write
"""

from __future__ import annotations

import argparse
import sys

from . import PLUGIN_DIR  # noqa: F401  —— 导入即把 core 加进 sys.path

from core import identify, jimaku, picker, placer
from core.settings import settings


def _print_header(text: str) -> None:
    print(f"\n{'=' * 60}\n{text}\n{'=' * 60}")


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，日文文件名会直接崩，强制 UTF-8
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="JaSubAuto M0：查 Jimaku 日语字幕")
    parser.add_argument("--tmdb-id", type=int, help="TMDB 剧集 ID")
    parser.add_argument("--season", type=int, default=1, help="TMDB 季号（默认 1）")
    parser.add_argument("--episode", type=int, required=True, help="TMDB 集号")
    parser.add_argument("--title", default="", help="罗马字/英文标题，仅在映射表查不到时用于兜底搜索")
    parser.add_argument("--anilist-id", type=int, help="直接指定 AniList ID，跳过识别")
    parser.add_argument("--video", default="", help="视频文件路径，用于计算字幕落盘位置")
    parser.add_argument("--write", action="store_true", help="真正写盘（默认 dry-run）")
    parser.add_argument("--all", action="store_true", help="列出全部候选，不只是前几个")
    args = parser.parse_args(argv)

    # ---------- 1. 识别 ----------
    _print_header("1. 番剧识别")
    if args.anilist_id:
        result = identify.Resolution(
            anilist_id=args.anilist_id,
            episode=args.episode,
            source="manual",
            confident=True,
            note="命令行直接指定 anilist_id",
        )
    else:
        if not args.tmdb_id:
            parser.error("必须提供 --tmdb-id 或 --anilist-id")
        result = identify.resolve(args.tmdb_id, args.season, args.episode, args.title)

    print(f"  来源      : {result.source or '（未识别）'}")
    print(f"  anilist_id: {result.anilist_id}")
    print(f"  换算后集号: {result.episode}")
    print(f"  可信      : {'是' if result.confident else '否'}")
    print(f"  说明      : {result.note}")
    if result.candidates and not result.anilist_id:
        print(f"  候选（{len(result.candidates)} 条）：")
        for row in result.candidates[:10]:
            print(f"    anilist={row.get('anilist_id') or row.get('id')} "
                  f"type={row.get('type') or row.get('format')} "
                  f"offset={(row.get('episode_offset') or {}).get('tmdb', 0)}")

    if not result.anilist_id:
        print("\n识别失败，无法继续。可用 --anilist-id 手动指定后重试。")
        return 1

    # ---------- 2. 查 Jimaku 条目 ----------
    _print_header("2. Jimaku 字幕条目")
    entries = jimaku.search_entries(result.anilist_id)
    if not entries:
        print(f"  Jimaku 上没有 anilist_id={result.anilist_id} 的字幕条目")
        return 1
    for e in entries:
        flags = e.get("flags", {})
        print(f"  entry={e['id']}  {e.get('name')}")
        print(f"      日文名 : {e.get('japanese_name')}")
        print(f"      英文名 : {e.get('english_name')}")
        print(f"      标记   : {'未审核 ' if flags.get('unverified') else ''}"
              f"{'剧场版 ' if flags.get('movie') else ''}最后更新 {e.get('last_modified')}")
    entry = entries[0]
    if len(entries) > 1:
        print(f"\n  注意：有 {len(entries)} 个条目，当前取第一个（entry={entry['id']}）")

    # ---------- 3. 候选字幕 ----------
    _print_header(f"3. 第 {result.episode} 集的候选字幕")
    files = jimaku.list_files(entry["id"])
    print(f"  该条目共 {len(files)} 个文件")
    candidates, chosen, reason = picker.pick(files, result.episode)
    print(f"  匹配到 {len(candidates)} 个候选 —— {reason}\n")
    show = candidates if args.all else candidates[:8]
    for i, c in enumerate(show, 1):
        mark = " ★ 自动选中" if chosen and c.name == chosen.name else ""
        print(f"  [{i}] {c.name}{mark}")
        print(f"      {c.lang_label} | 得分 {c.score} | {c.size:,} bytes | 更新于 {c.last_modified[:10]}")
        if c.reasons:
            print(f"      └ {'; '.join(c.reasons)}")
    if len(candidates) > len(show):
        print(f"  ...（还有 {len(candidates) - len(show)} 个，加 --all 看全部）")

    # ---------- 4. 落盘 ----------
    _print_header("4. 落盘")
    if not chosen:
        print("  未自动选中任何候选，按设计交给人工处理（M1 的网页会做这件事）")
        return 0
    if not args.video:
        print(f"  将下载：{chosen.name}")
        print("  未提供 --video，无法计算目标路径。加 --video <视频文件路径> 看完整结果")
        return 0

    # 命令行显式给了 --write 就以它为准（配置里的 DRY_RUN 是服务自动流程的默认值）
    content = jimaku.download(chosen.url) if args.write else None
    outcome = placer.place(args.video, chosen.name, content, dry_run=not args.write)
    print(f"  源文件  : {chosen.name}")
    print(f"  目标路径: {outcome.target}")
    print(f"  结果    : {outcome.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
