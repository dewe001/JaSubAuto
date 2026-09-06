# JaSubAuto

MoviePilot 插件：为番剧媒体库自动补日语字幕，字幕源 [jimaku.cc](https://jimaku.cc)。

MoviePilot 自带的字幕插件只覆盖中文字幕站点。本插件补上日语字幕，单个 V2 插件即可运行，
不依赖额外服务、容器或 Sonarr/Radarr。

## 功能

- **自动**：监听 `TransferComplete`，入库后为新剧集下载日语字幕。处理放在后台线程，不阻塞入库流程
- **手动**：插件自带网页，搜剧 → 批量补扫整季 → 试运行确认 → 写入。已有日语字幕的集数自动跳过
- **识别走 ID**：`tmdb_id` → 离线映射表 → `anilist_id`，正确处理 TMDB 与 AniList 的分季集号错位
- **不确定就不下载**：识别不可信、候选分不出优劣、季号为 0 的特典，一律标记待人工，不做猜测

## 安装

### 加入插件市场

插件市场地址由环境变量 `PLUGIN_MARKET` 控制（逗号分隔）。**该变量为整串覆盖**，
其默认值已包含官方仓库和三个常用第三方仓库，因此需要把它们连同本仓库一起写全：

```yaml
environment:
  - PLUGIN_MARKET=https://github.com/jxxghp/MoviePilot-Plugins,https://github.com/thsrite/MoviePilot-Plugins,https://github.com/honue/MoviePilot-Plugins,https://github.com/InfinityPacer/MoviePilot-Plugins,https://github.com/dewe001/JaSubAuto
```

若已自定义过该变量，在原值末尾追加 `,https://github.com/dewe001/JaSubAuto` 即可。
重启后在插件市场中安装「日语字幕补全（Jimaku）」。市场只读 `main` 分支。

### 本地目录（开发用）

```yaml
volumes:
  - /path/to/JaSubAuto:/plugins-dev
environment:
  - PLUGIN_LOCAL_REPO_PATHS=/plugins-dev
  - PLUGIN_AUTO_RELOAD=true
```

## 配置

必填两项：

| 项 | 说明 |
|---|---|
| Jimaku API Token | 在 <https://jimaku.cc/profile> 申请 |
| 番剧库目录 | 填到番剧那一层，如 `/媒体/日番`。填媒体库总根目录会把下载目录一并扫入 |

三个安全阀默认开启，建议逐个关闭而非一次全关：

- **探针模式**：只在日志打印事件内容，不做任何处理
- **试运行**：只显示将写入的路径与文件名，不落盘
- **剧集白名单**：填 `tmdb_id`，只处理名单内的剧；留空为不限制。仅约束自动模式

已存在的字幕文件在任何情况下都不会被覆盖。

## 落盘命名

```
葬送的芙莉莲 - S01E36 - 第 36 集.mkv
葬送的芙莉莲 - S01E36 - 第 36 集.ja.srt        ← 本插件写入
葬送的芙莉莲 - S01E36 - 第 36 集.zh-Hans.srt   ← MoviePilot 原有中文字幕，不受影响
```

规则为「视频同名 + `.ja` + 字幕原扩展名」，写在视频同目录。这是 Jellyfin / Emby / Plex
通用的外挂字幕约定。**本插件不与媒体服务器通信**，仅按约定写文件，写入后等扫库即可。

## 工作原理

季集编号是本项目的主要难点：TMDB 与 AniList 的分季体系不一致。以《葬送的芙莉莲》为例，
AniList 将两季拆为两个条目、各自从第 1 集编号，TMDB 则是单个 Season 1 连续编号至 38 集。

因此匹配全程使用 ID，不使用标题字符串（媒体库为中文名，标题匹配必然出错）：

```
媒体库文件 S01E37
  → tmdb_id 209867          （读剧集目录的 tvshow.nfo）
  → episode_offset 28       （离线映射表 Fribb/anime-lists）
  → anilist_id 182255 第 9 集
  → Jimaku 条目 Sousou no Frieren 2nd Season → 候选排序 → 写入
```

映射表查不到时回退到 AniList 标题搜索，但该结果**始终标记为不可信**且不自动下载——
回退路径没有偏移信息，判断错误就意味着给错集配了字幕。

## 字幕优先级

同一集通常有十余个候选。排序以语言纯度为主、片源偏好为辅：

| 类别 | 分值 | 示例 |
|---|---|---|
| 纯日语，无注释 | 300 | `[SubsPlease] ... _ja.srt` |
| 无语言标记 | 280 | AT-X/NTV 电视源、Moozzi2 BD |
| 纯日语，含 CC/SDH 注释 | 260 | `.ja[cc].srt`、`.ja-jp[sdh].srt` |
| 日中双语 | 200 | `JPSC`、`[CHS, JPN]` |
| 日英等双语 | 60 | `_ja-en.ass` |
| 不含日语 | 20 | — |

该权重面向日语学习：CC/SDH 为逐字听写、时间轴最准，但含 `（アイゼン）` 一类说话人标注与
音效描述，故排在干净台词之后。调整偏好修改 `core/picker.py` 的 `LANG_SCORES`。

同分时按配置的片源偏好排序；仍无唯一最优则拒绝自动下载，在网页上标记为待人工，可手动挑选。

## 开发

仓库按 MoviePilot 插件仓库布局组织：

```
package.v2.json                 插件索引，version 需与 plugin_version 一致
plugins.v2/jimakutrigger/       插件本体，目录名为主类名小写
├── __init__.py                 MoviePilot 集成层：事件监听 + get_api + 配置表单
└── core/                       业务逻辑，不依赖 Web 框架
icons/  service/  tests/        图标、脱机调试壳、测试
```

手动页面由插件 API 返回 HTML（`/api/v1/plugin/JimakuTrigger/ui`）：MoviePilot 以
`router.add_api_route(**api)` 注册插件接口，`response_class` 可透传，因此无需额外的 Web 服务。

`service/` 是脱机调试壳，导入同一份 `core/`，用于不启动 MoviePilot 时调试，生产环境不需要：

```bash
pip install -r requirements.txt
cp .env.example .env                       # 填 JIMAKU_API_TOKEN 与 MEDIA_ROOTS
uvicorn service.main:app --port 8990       # 页面与插件内的完全一致
python -m service.cli --tmdb-id 209867 --season 1 --episode 5
```

测试不联网：

```bash
python -m pytest tests/       # 核心逻辑 27 项
node tests/test_ui.mjs        # 手动页面 11 项，实际执行 ui.html 中的脚本
```

Fork 后需将 `package.v2.json` 中 `icon` 的仓库地址改为自己的，否则市场内不显示图标。

开发约定与实测结论见 [`CLAUDE.md`](./CLAUDE.md)。

## 限制

- 仅处理日语字幕，中文字幕由 MoviePilot 自身插件负责
- 不做翻译、时间轴校正、字幕烧录
- 全库定时补扫尚未实现，目前需按剧手动触发
- 映射表中同时含 `anilist_id` 与 `themoviedb_id` 的仅 6837 条，查不到属常态，手动模式为必需功能
- 目标为 MoviePilot V2。V3 可向下兼容加载 V2 插件，未实测

## 参考

- [Jimaku](https://jimaku.cc) · [API 文档](https://jimaku.cc/api/docs) · [站点源码](https://github.com/Rapptz/jimaku)
- [`jimaku-dl`](https://github.com/ksyasuda/jimaku-dl)：识别与选文件逻辑的参考实现
- [`Fribb/anime-lists`](https://github.com/Fribb/anime-lists)：ID 映射表，插件首次运行自动下载
- [MoviePilot V2 插件开发指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/V2_Plugin_Development.md)
- [Bazarr 的 Jimaku provider](https://github.com/morpheus65535/bazarr/pull/2505)：功能相近，
  但强制依赖 Sonarr/Radarr

## License

MIT
