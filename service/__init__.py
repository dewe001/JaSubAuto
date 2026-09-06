"""脱机调试壳子。

真正的实现在 plugins.v2/jasubauto/core/，插件和这里共用同一份代码——
只有一份实现，不存在"服务侧和插件侧行为不一致"的问题。

这个壳子存在的意义只有一个：不启动 MoviePilot 也能调核心逻辑。
生产环境只需要装插件，不需要跑这个。
"""

import os
import sys
from dataclasses import fields
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = PROJECT_ROOT / "plugins.v2" / "jasubauto"

if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from core import settings as core_settings  # noqa: E402


def bootstrap() -> None:
    """按 .env 配置核心，并把运行数据指向 service/data（映射表已经在那儿了）。

    进程环境变量优先于 .env——换掉 pydantic-settings 之后这层要自己做，
    否则 `MEDIA_ROOTS=... uvicorn ...` 这种临时覆盖会静默失效。
    """
    core_settings.load_env(PROJECT_ROOT / ".env")
    overrides = {f.name: os.environ[f.name.upper()]
                 for f in fields(core_settings.Settings)
                 if f.name.upper() in os.environ}
    if overrides:
        core_settings.configure(overrides)
    core_settings.settings.data_dir = PROJECT_ROOT / "service" / "data"


bootstrap()
