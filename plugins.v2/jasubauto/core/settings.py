"""核心配置。

刻意做成普通 dataclass 而不是 pydantic-settings：核心逻辑要能在 MoviePilot 进程内
直接跑，多一个依赖就多一分装不上的风险。两种填充方式：
  * 插件里：JaSubAuto.init_plugin() 拿到配置 dict 后调 configure()
  * 独立调试：load_env() 读项目根目录的 .env
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    # 必填
    jimaku_api_token: str = ""

    # 安全阀
    dry_run: bool = True
    series_whitelist: str = ""          # 调试期只处理这些 tmdb_id，空 = 不限制

    # 字幕选择
    subtitle_lang_suffix: str = "ja"
    fansub_whitelist: str = ""          # 片源偏好，次要因素；主因素是语言纯度
    subtitle_pref: str = "bilingual"    # bilingual=中日双语优先 / japanese=纯日语优先
    strip_annotations: bool = True      # 写盘前去掉说话人标注与音效描述
    merge_bilingual: bool = True        # 视频旁边有中文字幕时合成中日双语
    keep_japanese_only: bool = True     # 合并成功时另存一份纯日语，见 placer.RAW_TITLE

    # 媒体库
    media_roots: str = ""               # 浏览媒体库的起点，逗号分隔

    # 路径映射：插件模式下用不到（插件与 MoviePilot 同进程，看到的就是同一套路径），
    # 只有把核心逻辑跑在别的机器上时才需要
    path_mapping_from: str = ""
    path_mapping_to: str = ""

    # 运行数据（映射表 JSON）落在哪
    data_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "data")
    anime_lists_refresh_days: int = 7

    # ---- 派生属性 ----

    @property
    def whitelist_ids(self) -> list[int]:
        return [int(x) for x in self.series_whitelist.replace("，", ",").split(",")
                if x.strip().isdigit()]

    @property
    def preferred_keywords(self) -> list[str]:
        return [x.strip() for x in self.fansub_whitelist.replace("，", ",").split(",") if x.strip()]

    @property
    def media_root_list(self) -> list[str]:
        return [x.strip() for x in self.media_roots.replace("，", ",").split(",") if x.strip()]

    @property
    def anime_lists_file(self) -> Path:
        return Path(self.data_dir) / "anime-list-full.json"


settings = Settings()

_BOOL_FIELDS = {"dry_run", "strip_annotations", "merge_bilingual", "keep_japanese_only"}
_INT_FIELDS = {"anime_lists_refresh_days"}


def configure(values: dict) -> Settings:
    """用一个 dict 覆盖配置。未知键忽略，空字符串视为"没填"不覆盖默认值。"""
    for key, raw in (values or {}).items():
        key = key.lower()
        if not hasattr(settings, key):
            continue
        if key in _BOOL_FIELDS:
            setattr(settings, key, raw if isinstance(raw, bool) else
                    str(raw).strip().lower() in ("1", "true", "yes", "on"))
        elif key in _INT_FIELDS:
            try:
                setattr(settings, key, int(raw))
            except (TypeError, ValueError):
                pass
        elif key == "data_dir":
            if raw:
                setattr(settings, key, Path(raw))
        elif raw not in (None, ""):
            setattr(settings, key, str(raw))
    return settings


def load_env(path: str | Path) -> Settings:
    """独立调试用：读一个 KEY=VALUE 形式的 .env。生产路径（插件）不走这里。"""
    p = Path(path)
    if not p.is_file():
        return settings
    values: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        values[k.strip()] = v.strip().strip('"').strip("'")
    return configure(values)
