# JaSubAuto

给 MoviePilot 的番剧媒体库自动补**日语字幕**，字幕源为 [jimaku.cc](https://jimaku.cc)。

> MoviePilot 自带的字幕插件只抓中文字幕站点。本项目补上日语字幕这一环：新番入库后自动配好日文字幕，
> 存量剧集可按剧批量补齐，自动选不出来的留给网页手动挑。

**一个 MoviePilot V2 插件，装上即用，不需要额外的服务、容器或 *arr 全家桶。**

## 安装

### 方式一：作为插件仓库添加（推荐）

MoviePilot 的插件市场地址由环境变量 `PLUGIN_MARKET` 控制，多个用逗号分隔。把本仓库加进去：

```yaml
environment:
  - PLUGIN_MARKET=https://github.com/jxxghp/MoviePilot-Plugins,https://github.com/dewe001/JaSubAuto
```

重启后在插件市场里就能看到「日语字幕补全（Jimaku）」。**市场只读 `main` 分支。**

### 方式二：本地目录（开发调试用）

```yaml
volumes:
  - /path/to/JaSubAuto:/plugins-dev
environment:
  - PLUGIN_LOCAL_REPO_PATHS=/plugins-dev
  - PLUGIN_AUTO_RELOAD=true
```

### 装完之后

填两项：[Jimaku API Token](https://jimaku.cc/profile) 和番剧库目录（填到番剧那一层，
如 `/媒体/日番`，别填媒体库总根目录，否则下载目录也会被扫进来）。

首次使用保持默认的「探针模式」和「试运行」开启：先在日志里确认识别结果和将要写入的路径，
确认无误再逐个关掉。**已存在的字幕文件永远不会被覆盖。**

插件详情页里有「打开日语字幕手动页面」的链接——找剧、批量补扫、手动挑字幕都在那儿。

## 怎么用

**自动**：MoviePilot 入库完成后触发，为新入库的剧集补日语字幕。识别不确定或候选分不出优劣时
只记日志、不下载，绝不瞎猜。

**手动**（插件详情页 → 手动页面）：输入剧名的几个字 → 选中那部剧 → 试运行看清单 →
确认写入。已有日语字幕的集数自动跳过。

## 落盘效果

```
葬送的芙莉莲 - S01E36 - 第 36 集.mkv
葬送的芙莉莲 - S01E36 - 第 36 集.ja.srt        ← 本项目新增
葬送的芙莉莲 - S01E36 - 第 36 集.zh-Hans.srt   ← MoviePilot 原有的中文字幕，不受影响
```

命名规则是「视频同名 + `.ja` + 字幕原扩展名」，放在视频同目录。这是 Jellyfin / Emby / Plex
通用的外挂字幕约定，媒体服务器靠它自动识别字幕语言，不需要任何额外配置。

> 本项目**不与媒体服务器通信**，只按约定写文件。用什么媒体服务器都行，只要它认这个命名约定。
> 写入后等媒体服务器扫库（或手动刷新一下）即可看到日语字幕轨。

## 字幕取舍规则

同一集常有十来个候选（不同字幕组、片源、语言组合）。排序以**语言纯度**为主、片源偏好为辅：

| 类别 | 分值 | 实例 |
|---|---|---|
| 纯日语、干净台词 | 300 | `[SubsPlease] ... _ja.srt` |
| 无语言标记（Jimaku 上大概率纯日语） | 280 | AT-X/NTV 电视源、Moozzi2 BD |
| 纯日语，但含 CC/SDH 听障注释 | 260 | `.ja[cc].srt`、`.ja-jp[sdh].srt` |
| 日中双语 | 200 | `JPSC`、`[CHS, JPN]` |
| 日英等其它双语 | 60 | `_ja-en.ass` |
| 不含日语 | 20 | — |

这套权重是为**看剧学日语**设的：CC/SDH 逐字听写、时间轴最准，但夹带 `（アイゼン）` 这类
说话人标注和音效描述，所以排在干净台词之后。想要别的偏好改 `core/picker.py` 的 `LANG_SCORES` 即可。

同分时按配置的「片源偏好」打破平局；**仍分不出唯一最优就拒绝自动下载**，在手动页面标为「待人工」，
点一下就能自己挑。

## 它是怎么找到字幕的

番剧的季集编号是这个项目最大的坑：TMDB 和 AniList 的分季体系不一致。以《葬送的芙莉莲》为例，
AniList 把两季拆成两个条目各自从第 1 集数起，TMDB 则是一个 Season 1 连续编号到 38 集。

所以匹配主路径全程走 ID，不用标题字符串（媒体库是中文名，标题匹配必错）：

```
媒体库文件名 S01E37
  → tmdb_id 209867（从剧集目录的 tvshow.nfo 读）
  → 离线映射表 Fribb/anime-lists，命中 episode_offset=28
  → anilist_id 182255 第 9 集
  → Jimaku 条目「Sousou no Frieren 2nd Season」→ 候选排序 → 落盘
```

映射表查不到时退回 AniList 标题搜索，但结果**永远标记为不可信**、不自动下载
（无偏移信息，猜错就是给错集配字幕）。季号为 0 的特典同理，一律交人工。

## 架构

全部代码跑在 MoviePilot 进程内，一个插件：

```
plugins.v2/jimakutrigger/
├── __init__.py          MoviePilot 集成层
│                          · 监听 TransferComplete → 自动补字幕（后台线程，不阻塞入库）
│                          · get_api() 注册接口，其中 /ui 直接返回手动页面的 HTML
│                          · get_form() 配置项 / get_page() 详情页入口
└── core/                业务逻辑，不依赖任何 Web 框架
    identify.py            tmdb_id → anilist_id，处理分季集号错位
    jimaku.py              Jimaku API 客户端（25 请求/分钟限速器）
    picker.py              候选字幕排序与取舍
    placer.py              定位视频 → 命名 → 落盘（绝不覆盖）
    scan.py                单集处理 + 整部剧批量补扫
    library.py             按剧名搜媒体库、读 tvshow.nfo 取 tmdb_id
    ui.html                手动页面（原生 HTML + fetch，无前端框架）
```

手动页面挂在 `/api/v1/plugin/JimakuTrigger/ui`——MoviePilot 注册插件接口的实现是
`router.add_api_route(**api)`，`response_class` 可以透传，所以插件能自己返回 HTML，
不必另起一个 Web 服务。

## 仓库结构

按 MoviePilot 官方插件仓库的布局组织，可以直接作为第三方插件市场使用：

```
package.v2.json                 插件索引（version 必须和 plugin_version 一致）
plugins.v2/jimakutrigger/       插件本体，目录名 = 主类名小写
icons/jimakutrigger.png         图标
service/  tests/                开发用，MoviePilot 不读
```

> ⚠️ Fork 或改名后记得把 `package.v2.json` 里 `icon` 的 GitHub 用户名换成你自己的，
> 否则插件市场里显示不出图标。

## 开发

`service/` 是**脱机调试壳子**，import 同一份 `core/`，用于不启动 MoviePilot 时调逻辑，生产不需要：

```bash
pip install -r requirements.txt
cp .env.example .env                       # 填 JIMAKU_API_TOKEN 和 MEDIA_ROOTS
uvicorn service.main:app --port 8990       # 界面与插件页面完全一样
python -m service.cli --tmdb-id 209867 --season 1 --episode 5
```

测试都不联网，秒级跑完：

```bash
python -m pytest tests/       # 核心逻辑 27 项
node tests/test_ui.mjs        # 手动页面 11 项（真的把 ui.html 的脚本跑起来）
```

开发约定、实测结论和踩过的坑见 [`CLAUDE.md`](./CLAUDE.md)。

## 现状与限制

- 自动模式的事件字段已在真实 MoviePilot V2 上核对过；手动模式（搜剧 → 批量补扫 → 落盘）已实际使用
- **只做日语字幕**，中文字幕交给 MoviePilot 自己的插件
- 不做翻译、时间轴校正、字幕烧录
- 全库定时补扫还没做（目前是按剧手动触发）
- 映射表里同时有 anilist_id 和 tmdb_id 的只有 6837 条，查不到是常态——所以手动模式不是可选项而是必需品

## 前置条件

- MoviePilot **V2**（V3 能向下兼容加载 V2 插件，但没实测过）
- [Jimaku API Token](https://jimaku.cc/profile)（限速 25 请求/分钟）
- 番剧 ID 映射表 [`Fribb/anime-lists`](https://github.com/Fribb/anime-lists) 的
  `anime-list-full.json`（7.5MB，插件首次运行自动下载到插件数据目录）

## 参考

- [Jimaku](https://jimaku.cc) · [API 文档](https://jimaku.cc/api/docs) · [站点源码](https://github.com/Rapptz/jimaku)
- [`jimaku-dl`](https://github.com/ksyasuda/jimaku-dl)：识别与选文件逻辑的参考实现
- [MoviePilot V2 插件开发指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/V2_Plugin_Development.md)
- [Bazarr 的 Jimaku provider](https://github.com/morpheus65535/bazarr/pull/2505)：功能类似，
  但强制依赖 Sonarr/Radarr，本项目不走这条路
