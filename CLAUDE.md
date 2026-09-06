# CLAUDE.md

本文件供 Claude Code 在本仓库工作时参考。**动代码前先读「边界」和「硬性约束」两节。**

## 项目是什么

JaSubAuto：给 MoviePilot 的番剧媒体库自动/半自动补**日语字幕**，字幕源是 [jimaku.cc](https://jimaku.cc)。
中文字幕由 MoviePilot 现有插件负责，本项目不碰。

**与 Jellyfin 的关系**：只有落盘命名这一处。`<视频名>.ja.srt` 是 Jellyfin/Emby/Plex 通用的
外挂字幕约定，本项目按它写文件，**不调用任何媒体服务器 API**（早期设计里有过"下载后通知
Jellyfin 刷新"的可选项，实现时没做——等扫库就够了，不值得为它引入一套地址+密钥配置）。

## 边界（避免发散的核心）

**做：**
- 从 Jimaku 拿日语字幕，按 Jellyfin 命名约定放到视频文件旁边
- 自动模式：MoviePilot 入库后触发
- 手动模式：网页上选剧 → 看候选字幕 → 勾选下载
- 存量补扫：找出库里缺日语字幕的剧集

**不做（除非用户明确要求）：**
- **不引入 Sonarr / Radarr / Bazarr**。Bazarr 确实内置了 Jimaku provider（[PR #2505](https://github.com/morpheus65535/bazarr/pull/2505)），但它**强制依赖 Sonarr/Radarr 来提供媒体库信息**——为了拿日语字幕而在 MoviePilot 之外再养一套 *arr 全家桶，等于让两套系统同时管同一个媒体库，收益远小于代价。MoviePilot 已经有识别结果（tmdb_id）和入库事件，直接用就行。
- 不做字幕翻译、时间轴校正/同步、字幕烧录
- 不做中文字幕相关功能
- 不做多用户、权限、账号体系（单人自用）
- 不写 Vue 联邦前端插件页（手动 UI 是插件 API 直接返回的一张原生 HTML，不用 Vuetify JSON 拼交互）
- 不再拆出独立服务/容器（拆过又合回来了，理由见「架构」一节）
- **暂不引入 LLM**：集号解析（302/305 命中，未命中的 3 个是特别节目，属正确行为）和 ID 映射（确定性查表）都不是理解问题，规则可复现、可解释、可调试，LLM 只会增加延迟和幻觉风险。唯一可能有价值的位置是「映射表查不到时的标题兜底消歧」——若日后实测该路径误判率高，再作为可选 refiner 接在 `identify.search_anilist` 后面，接口先留着不实现。

## 架构：一个插件，装上即用

**全部代码跑在 MoviePilot 进程内的插件里，不需要额外的服务或容器。**

```
plugin/plugins.v2/jimakutrigger/
├── __init__.py     MoviePilot 集成层：事件监听 + get_api + get_form + get_page
└── core/           业务逻辑，不依赖任何 Web 框架
    ├── settings.py   普通 dataclass 配置（插件传 dict / 调试读 .env）
    ├── http.py       HTTP 垫片：有 httpx 用 httpx，否则用 requests
    ├── identify.py   tmdb_id → anilist_id + 集号换算
    ├── jimaku.py     Jimaku API 客户端（含限速）
    ├── picker.py     候选字幕排序与取舍
    ├── placer.py     视频定位 + 落盘 + 覆盖保护
    ├── scan.py       单集处理 process_one + 整部剧批量补扫 scan
    ├── library.py    浏览媒体库、读 tvshow.nfo 取 tmdb_id
    └── ui.html       手动页面（原生 HTML+fetch，无前端框架）
```

**为什么手动页面能塞进插件**：MoviePilot 注册插件 API 的实现是 `router.add_api_route(**api)`，
整个 dict 直接展开成 FastAPI 参数，所以 `"response_class": HTMLResponse` 可以透传。
页面挂在 `/api/v1/plugin/JimakuTrigger/ui`，用 `auth: "apikey"`（浏览器直接打开带不了 Bearer 头）。
`get_page()` 只放一个跳转链接，不用 Vuetify JSON 拼交互界面。

### 一度走过的弯路（别再走回去）

早期版本拆成「薄插件 + 独立 FastAPI 服务（Docker）」，理由是依赖重、要故障隔离、要 HTML 页面。
后来推翻了，三条理由只有一条成立且已被绕开：

- *依赖重*：fastapi/uvicorn 是**因为要独立起服务才需要的**，循环论证；sqlite 至今没用上；
  httpx 是在 Windows 上踩 SSL 坑后选的，那是开发机问题，容器里 `requests` 就够。净增依赖为零。
- *故障隔离*：事件处理整个包在 try/except 里，实际工作还丢进后台线程，不阻塞入库流程。
- *HTML 页面*：已证伪，见上。

合并后额外消掉的东西：路径映射（插件与 MoviePilot 同进程，看到的就是同一套路径）、
服务访问令牌、防火墙放行、服务地址配置。**而且别人装这个插件只需要点一下安装，不必再起一个容器。**

`service/` 保留为**脱机调试壳子**（几十行的 FastAPI 包装，import 同一份 `core/`），
用途是不启动 MoviePilot 也能调逻辑。生产不需要它，接口路径与插件一一对应，共用同一份 `ui.html`。

### 目标版本：MoviePilot V2

插件按 **V2** 规范写（用户暂不升级 V3）：

- 插件目录：`plugins.v2/<插件id小写>/__init__.py`，索引写 `package.v2.json`
- import 用 V2 路径：`app.core.event`（`eventmanager` / `Event`）、`app.plugins._PluginBase`、`app.schemas.types.EventType`、`app.log.logger`
- **本插件零额外依赖**，不需要 `requirements.txt`
- 权威文档：[V2 插件开发指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/V2_Plugin_Development.md)
- V3 能回退兼容加载 V2 插件

## 硬性约束

- **Jimaku API 认证**：`Authorization: <token>`，**不带 `Bearer ` 前缀**（已对照 jimaku-dl 源码核实）
- **限速 25 请求/分钟/Key**，所有调用必须走统一的节流器，批量补扫尤其注意
- **匹配主路径是 ID**：`tmdb_id` → 离线映射表 → `anilist_id` → Jimaku。标题字符串搜索只作兜底，绝不作为主路径（媒体库是中文名，标题匹配必错）
- **中文剧名不能拿去搜 AniList**：实测「咒术回战」「间谍过家家」能搜到（synonyms 里恰好有中文），但「葬送的芙莉莲」搜到 0 条——时灵时不灵比全都搜不到更危险。手动模式按中文名选剧走的是 `library.py`：浏览媒体库目录 → 读剧集根目录的 `tvshow.nfo` 拿 `tmdb_id`（MoviePilot 刮削产物，含 `<uniqueid type="tmdb">` 或 `<tmdbid>`）→ 走正常的 ID 主路径。没有 nfo 就降级为人工填 tmdb_id，不猜。
- **落盘命名**：`<视频文件basename>.ja<原扩展名>`，与视频同目录。Jellyfin 靠这个约定识别语言
- **字幕偏好顺序固定**（用途是学日语，不是无障碍观看）：干净纯日语台词 > 无语言标记 > 纯日语但含 CC/SDH 听障注释 > 日中双语 > 日英等 > 不含日语。CC/SDH 是逐字听写，时间轴最准，但夹带 `（アイゼン）` 说话人标注和音效描述，所以排在干净台词之后
- **绝不覆盖已存在的字幕文件**：写盘前必须 `exists()` 检查，已有就跳过
- **匹配不唯一就不下载**：多个字幕组且白名单分不出优先级、或只有合集文件 → 标记「待人工确认」，交给手动模式，不要瞎猜
- **所有写盘操作必须支持 dry-run，且 dry-run 是默认值**
- **失败必须静默降级**：找不到字幕、API 报错、超时，都只记日志，绝不抛异常影响调用方

## 已验证的外部接口事实（M0 实测，不要再猜）

### Jimaku API

```
GET /api/entries/search?anilist_id=154587
 -> [{"id":729, "name":"Sousou no Frieren", "anilist_id":154587,
      "english_name":"...", "japanese_name":"葬送のフリーレン",
      "flags":{"anime":true,"unverified":false,"external":true,"movie":false,"adult":false},
      "last_modified":"2026-02-14T00:37:40Z"}]

GET /api/entries/729/files
 -> [{"url":"https://jimaku.cc/entry/729/download/<urlencoded>", "name":"...",
      "size":23195, "last_modified":"2024-03-03T14:43:52Z"}]
```

`url` 是可直接 GET 的下载直链。认证头 `Authorization: <token>`，**不带 Bearer**。

### 映射表：必须用 `anime-list-full.json`

`Fribb/anime-lists` 有多个文件，**只有 `anime-list-full.json`（7.5MB）同时含 `anilist_id` + `themoviedb_id` + `episode_offset`**。`anime-lists-reduced.json` 没有 anilist_id，不能用。

结构（`themoviedb_id` 是对象不是标量）：
```json
{"type":"TV", "anidb_id":18886, "anilist_id":182255,
 "themoviedb_id":{"tv":209867}, "tvdb_id":424536,
 "season":{"tvdb":2,"tmdb":1}, "episode_offset":{"tmdb":28}}
```

覆盖率实测：42870 条中 20687 条有 anilist_id，但**同时有 anilist_id 和 themoviedb_id.tv 的只有 6837 条**。所以映射表查不到是常态，标题兜底和手动模式都是必需品，不是可选项。

### 核心算法：TMDB↔AniList 集号换算（`identify.py`）

TMDB 和 AniList 的分季体系不一致。实测《葬送のフリーレン》：

| | AniList | TMDB |
|---|---|---|
| 第一季 | id=154587，第 1-28 集 | S1E1-E28 |
| 第二季 | id=182255，第 1-10 集 | **仍是 S1**，接在 E29 之后 |
| 第三季 | id=209939 | 映射表里**完全没有** tmdb 对应 |

所以 `(tmdb_id, season)` 不是唯一键（实测 5940 个键中 373 个即 6% 有歧义，且歧义几乎全部集中在 `season=0` 的特典/OVA）。算法：

1. 按 `(tmdb_id, tmdb_season)` 取出所有候选行
2. `season == 0` 直接判为不可信（特典编号体系不可靠），交人工
3. 否则按 `episode_offset.tmdb` **从大到小**试，取第一个满足 `集号 - offset >= 1` 的行
4. 换算后的集号才是拿去和 Jimaku 文件名匹配的集号
5. 映射表查不到 → AniList 标题搜索兜底，但**结果永远标记为 not confident**（无偏移信息）

已验证：`S1E29` 正确换算为 `anilist=182255 第 1 集`，并找到第二季的 Jimaku 条目。

### TransferComplete 事件的真实结构（2026-09-06 实测，部分待补）

顶层键：`['fileitem', 'meta', 'mediainfo', 'transferinfo', 'downloader', 'download_hash', 'transfer_history_id']`

```python
mediainfo    = {'source': 'themoviedb', 'media_id': '209867', 'type': '电视剧',
                'title': '葬送的芙莉莲', 'en_title': "Frieren: Beyond Journey's End", ...}
transferinfo = {'success': True,
                'fileitem': {'path': '/媒体/下载/日番/[Sakurato] ... S2 [09] ....mkv', ...}, ...}
```

两个坑：

- **`media_id` 是字符串**（`'209867'`），且实测日志里没直接看到 `tmdb_id`。`_tmdb_id()` 两个字段都试，
  取 `media_id` 时会先确认 `source == 'themoviedb'`。
- **`transferinfo.fileitem` 是下载源路径**（`/媒体/下载/...`），**不是入库后的路径**。
  拿它当目标会把字幕写到下载目录去。`_target_files()` 只认 `file_list_new` / `file_list` /
  `target_item` / `target_path`，绝不回退到 `fileitem`。

**入库后的路径**（第二次探针实测）：

```
/媒体/日番/葬送的芙莉莲 (2023)/Season 1/葬送的芙莉莲 - S01E36 - 第 36 集.mkv
```

即 `<番剧库>/<剧名 (年份)>/Season N/<剧名> - SxxExx - 第 N 集.mkv`。注意文件名里
**同时有 `S01E36` 和「第 36 集」**，`scan.parse_video()` 的 `S01E36` 模式优先级最高，实测解析为 (1, 36)。

用户的番剧库根目录是 **`/媒体/日番`**，`/媒体` 下面还有 `下载`、电影等——
所以「番剧库目录」配置项必须填到 `/媒体/日番` 这一层，填 `/媒体` 会把下载目录也扫进来。

### 分季：以 TMDB 的编号为准，不是字幕组的

《葬送的芙莉莲》在 TMDB 上**只有一个 Season 1（38 集）**，没有独立第二季（2026-09-06 查证）。
所以第二季第 9 集在媒体库里叫 `S01E37`（28 + 9），哪怕下载来的文件名写的是 `S2 [09]`。
映射表里 `tmdb=209867` 也只有 `season=1` 的两行（offset=0 和 offset=28），两边是自洽的。

实测 `S01E37 → anilist=182255 第 9 集`，Jimaku 条目 `Sousou no Frieren 2nd Season`(11446) 有对应字幕。

**推论**：如果哪天媒体库里出现了 `S02Exx` 而映射表只有 `season=1`，识别会返回 unidentified（不是下错），
此时走手动页面用 AniList ID 补——那条路径直接拿文件名集号当 AniList 集号，正好对得上。

### 两个环境坑（Windows）

- **stdlib `urllib` 连 jimaku.cc 会报 `CERTIFICATE_VERIFY_FAILED: certificate has expired`**，因为它走 Windows 证书库；`httpx`（走 certifi）没问题。**统一用 httpx，不要用 urllib/requests。**
- Windows 控制台默认 GBK，打印日文文件名会崩。CLI 入口已 `sys.stdout.reconfigure(encoding="utf-8")`，新写的入口也要加。
- Git Bash 里 `pip` 不在 PATH，用 `python -m pip`；Git Bash 的 `/tmp` 和 Windows Python 看到的路径不是一回事。

### 字幕文件名的真实形态

同一集常有 10 个左右候选，实测来源包括：`[SubsPlease]`（_ja.srt / _ja-en.ass 双语）、`[Moozzi2]`（BD）、`[NanakoRaws]`、`[erai-raws-timed]`、`[Nekomoe kissaten]`（JPSC 日简双语）、Netflix/Amazon 官方 WEBRip（`.ja[cc]` / `.ja-jp[sdh]`）、电视台源（AT-X/NTV）。

集号解析实测 305 个文件命中 302 个，未命中的 3 个是特别节目（`×ZIP！SP`、`フラアニ特別編`），属正确行为。注意 `[01]` 这种方括号集号必须支持（字幕组常用），而 `[8E3F8FA5]` 这种 CRC 不能误判成集号。

## 技术选型（定了就别换）

- Python 3.12
- 插件：零额外依赖（标准库 + MoviePilot 已有的 requests）
- HTTP：`core/http.py` 垫片，有 httpx 用 httpx，否则 requests。**不要用 stdlib urllib**（Windows 上连 jimaku.cc 报证书过期）
- 脱机调试壳子：FastAPI + uvicorn，仅开发用
- 状态：目前不需要持久化，没引 sqlite（真要加再说，别提前上）
- 手动 UI：一个服务端返回的单页 HTML + 原生 fetch，**不引前端框架**
- 配置：插件配置界面（生产）/ `.env`（调试）→ 统一进 `core/settings.py` 的普通 dataclass，不用 pydantic-settings
- 依赖尽量少：能用标准库解决的不加包

## 配置与密钥

**生产配置全在 MoviePilot 的插件配置界面里填**，不落地到仓库里的任何文件：
Jimaku Token、媒体库根目录、剧集白名单、片源偏好、试运行开关、探针开关。

`.env` / `.env.example` 只服务于脱机调试壳子（`service/`），真实 `.env` 不进仓库。
不要把任何密钥写进代码、写进插件配置的默认值、或提交进仓库。

## 目录结构

```
JaSubAuto/                       # 本身就是一个 MoviePilot 插件仓库，布局与官方一致
├── package.v2.json                # 插件索引，version 必须与 plugin_version 一致
├── icons/jimakutrigger.png
├── plugins.v2/jimakutrigger/      # 项目主体，目录名 = 主类名小写
│   ├── __init__.py                # MoviePilot 集成层
│   ├── core/                      # 业务逻辑（见上面的架构图）
│   └── data/                      # 运行数据（映射表 JSON）——不进仓库
├── service/                       # 脱机调试壳子，生产不需要
│   ├── __init__.py                # 把 core/ 加进 sys.path + 读 .env
│   ├── main.py                    # FastAPI 包装，接口路径对应插件 API
│   └── cli.py                     # 命令行单集查询
├── tests/
│   ├── test_core.py               # pytest，用 fixtures 里的迷你映射表，不联网
│   ├── test_ui.mjs                # node，真跑 ui.html 的脚本
│   └── fixtures/anime-list-mini.json
├── .env.example                   # 只给 service/ 用；插件配置在 MoviePilot 界面里填
├── LICENSE / README.md / CLAUDE.md
```

## 开发调试流程（按阶段推进，不要跳阶段）

核心原则：**把"能不能拿到字幕"和"能不能接上 MoviePilot"分开调**。80% 的代码不需要碰 MoviePilot。

- **M0 命令行跑通核心链路** ✅ 已完成（2026-09-06）：`python -m service.cli --tmdb-id 209867 --season 1 --episode 5`。识别、集号换算、候选筛选、偏好排序、落盘路径计算全部验证通过；实测结论见上面「已验证的外部接口事实」。
- **M1 手动网页** ✅ 已完成（2026-09-06）：脱机调试 `uvicorn service.main:app --port 8990`；生产是插件 API `/api/v1/plugin/JimakuTrigger/ui`。两边同一份 `ui.html`。
- **M2 模拟入库事件** ✅ 已完成（2026-09-06）：`/api/jobs` 用模拟 payload 验证过（白名单、season=0、缺路径、非视频文件、已有字幕、跨季换算等边界）。
- **M3 触发器插件** 🔶 代码已写好（`plugin/plugins.v2/jimakutrigger/`），**默认开启「探针模式」只打日志不转发**。还没装到 MoviePilot 上验证过，`_build_payload()` 里的字段名属于待核对状态。装上去触发一次整理，从日志里核对字段，确认后关掉探针模式。不要对着文档猜字段。

  插件转发的 payload 是 `{tmdb_id, title, type, files: [视频路径...]}`——**故意不传季集号**，由服务侧用 `scan.parse_video()` 从文件名解析，和批量补扫共用同一套代码。这样插件只依赖 `tmdb_id` 和文件列表两个字段，MoviePilot 改 meta 语义也不会跟着坏。

  **装法（Synology Docker）**：把仓库整个拷到 NAS（如 `/volume3/docker/JaSubAuto/`），在 MoviePilot 容器上加挂载 `/volume3/docker/JaSubAuto:/plugins-dev`，加环境变量 `PLUGIN_LOCAL_REPO_PATHS=/plugins-dev` 和 `PLUGIN_AUTO_RELOAD=true`，重启容器 → 插件市场里能看到「日语字幕补全（Jimaku）」。插件配置里填 Jimaku Token 和番剧库目录 `/媒体/日番` 即可，没有服务地址/令牌这类东西。
- **M4 路径映射 + 存量补扫**：路径映射已确认不需要（见「部署环境」）。单文件夹批量补扫 ✅ 已完成（`core/scan.py` + `POST .../scan`）：递归找视频 → 解析季集 → 跳过已有 `.ja.*` 的 → 每个 AniList 条目只查一次 Jimaku 文件列表（限速）→ 逐集落盘。剩余：全库定时扫描。

### MoviePilot 侧调试操作（已查证）

- **装插件调试**：环境变量 `PLUGIN_LOCAL_REPO_PATHS`（本地插件仓库目录，多个用 `,` 分隔）+ `PLUGIN_AUTO_RELOAD=true`（热重载）。Docker 部署需把宿主目录挂进容器。
- ⚠️ `DEV=true` 也能热重载，但**会暂停所有定时任务**，不要在日常使用的实例上长期开启。
- **按需触发转移**（不用等新番）：历史记录 → 选中记录 → 重新整理。（是否会再次抛 `TransferComplete` 事件需要实测确认，这是装上插件后第一件要验证的事。）
- **看日志**：`LOG_LEVEL=DEBUG`；MoviePilot 后台实时日志页 / `docker logs -f` / `CONFIG_DIR`（Docker 通常 `/config`）下的日志文件。
- 如果 `PLUGIN_LOCAL_REPO_PATHS` 不生效，退路是把插件目录直接塞进容器的插件目录再重启（较笨但一定有效）。

### 三个安全阀（调试期必须开着）

1. `dry_run` 默认 `true`，只打印不写盘
2. 调试期用**剧集白名单**，只允许处理指定的一两部剧，防止 bug 把错字幕撒满全库
3. 绝不覆盖已存在字幕文件

## 部署环境（已确认，2026-09-06）

| 项 | 值 |
|---|---|
| MoviePilot | NAS 上的 Docker，`http://192.168.1.23:23000` |
| 媒体库挂载 | `/volume3/媒体:/媒体`（宿主机 : 容器内） |
| 部署方式 | **只装插件**，没有别的进程/容器 |

**不需要路径映射**：插件与 MoviePilot 同进程，拿到的路径就是容器内的 `/媒体/...`，直接可用。
`path_mapping_*` 只在把 `core/` 跑到别的机器上时才有意义（即调试壳子），生产路径用不到。

**装法**：把 `plugin/` 拷到 NAS（如 `/volume3/docker/subpick-plugin/`），MoviePilot 容器加挂载
`/volume3/docker/subpick-plugin:/plugins-dev` + 环境变量 `PLUGIN_LOCAL_REPO_PATHS=/plugins-dev`、
`PLUGIN_AUTO_RELOAD=true`，重启即可在插件市场看到。
将来发给别人用就把 `plugin/` 推成一个 GitHub 插件仓库，对方填仓库地址装即可。

## 待确认信息（写相关代码前先问用户）

- [x] ~~MoviePilot 版本~~ → **已确认 V2**（2026-09-06，用户选择观望 V3 稳定后再升级）
- [x] ~~部署方式 / 宿主机 / 路径映射~~ → 见上面「部署环境」（2026-09-06）
- [ ] Jellyfin 地址 / API Key（用于下载后触发刷新，可选功能）
- [x] ~~`TransferComplete` 的 payload 字段~~ → 已全部实测确认，见上
- [x] ~~媒体库结构~~ → 番剧库是 `/媒体/日番`，布局 `<剧名 (年份)>/Season N/<剧名> - SxxExx - 第 N 集.mkv`

## 常用命令

生产只需装插件，下面都是脱机调试用的：

```bash
pip install -r requirements.txt        # 只装调试壳子需要的 fastapi/uvicorn/httpx

# 调试壳子（界面与插件页面完全一样）
uvicorn service.main:app --port 8990   # 浏览器打开 http://127.0.0.1:8990

# 命令行查单集
python -m service.cli --tmdb-id 209867 --season 1 --episode 5

# 批量补扫一整部剧（默认 dry-run，加 --write 才真写）
python -m core.scan --dir "/媒体/动漫/某剧" --tmdb-id 209867

# 跑测试
pytest tests/
```

## 手动页面的交互原则

**是「搜索」不是「浏览」。** 番剧库动辄几百部，一进页面就把整库铺出来没法用，加筛选框也只是治标。
所以 `library.find_shows()` 在关键字为空时**只报总数、不返回任何剧**，输了关键字才列匹配项。
统计集数要递归扫目录，也只对匹配到的（≤30 部）做。手工指定目录的 `browse()` 收进折叠区兜底。

## 交付纪律（每次让用户去测之前必须做完）

1. **跑测试**：改了 `core/` 跑 `python -m pytest tests/`；改了 `ui.html` 跑 `node tests/test_ui.mjs`。
   两个测试都不联网，几百毫秒跑完，没有理由跳过。
2. **bump 版本号**：`plugin_version` 和 `package.v2.json` 的 `version` 同时改，并在 `history` 里写清改了什么。
   不 bump 的话 MoviePilot 认不出这是新版本，用户测的可能还是旧代码——这个坑已经踩过，
   而且当时被误判成"功能没做对"，白白多绕了两轮。
3. **版本号语义**：功能没在真机上验证通过之前不许上 `1.0`。测试期一律 `0.x.y`。
4. 页面右上角有版本戳（版本号 + `ui.html` 的 mtime），部署后先看它确认加载的是哪一版，
   再判断功能对不对。这条比 MoviePilot 的版本比对更可靠，因为它直接来自文件本身。

### 前端代码的硬性要求

**内联 `onclick` 里绝不许拼模板变量。** 路径和字幕文件名里有引号、括号、反斜杠，
`onclick="f(${JSON.stringify(x)})"` 会让属性提前闭合、整行渲染成垃圾——已经出过一次事故。
一律用 `data-*` 属性（值过 `esc()`）+ 事件委托。`tests/test_ui.mjs` 里有条 lint 会挡住这种写法。

## 写代码时的注意事项

- 网络调用（Jimaku / AniList / Jellyfin）在测试里必须 mock，单测不依赖公网
- 日志要能回答"为什么这一集没下到字幕"：识别到哪个 anilist_id、拿到几个候选、按什么规则筛掉的，都要打出来
- 番剧的季集编号错位（分季、总集篇、OVA、剧场版）是最大的坑，宁可标记待确认也不要下错
- **`season` 不能用 `x or 1` 取默认值**：季号 0 是合法值（特典），falsy 判断会把它变成第 1 季，等于给特典配正片字幕。已在 `main.create_job` 踩过一次，用 `if x is not None` 判断。
- 路径类入参（`video_path` 等）必须先判空再进 `pathlib`：`Path("")` 会变成 `WindowsPath('.')`，`.with_name()` 直接抛 `ValueError` 导致 500，违反"失败必须静默降级"。
- **`video_path` 是目录时不能直接 `.with_name()`**：`Path("D:/某目录").with_name(stem + ".ja.srt")` 会算出该目录的**同级兄弟**（`D:/某目录.ja.srt`），静默产出垃圾路径。一律先过 `placer.resolve_video()`：目录就按媒体库集号在里面找唯一视频，找不到或不唯一就拒绝。
- **区分两种集号**：`library_episode`（媒体库/TMDB 口径，视频文件名里写的那个）用来在目录里找视频；`Resolution.episode`（AniList 口径）用来匹配 Jimaku 字幕文件。分季错位的番里两者不相等（媒体库 S01E29 = AniList 第二季第 1 集），传错就会给第 29 集配上第 1 集的字幕。
- 改完服务端代码要重启才生效；旧进程占着端口时新进程会静默 bind 失败（请求仍打到旧代码，看起来像"改了没用"）。用 `netstat -ano | grep :8990` 找 PID 再 `taskkill //F //PID <pid>`。
