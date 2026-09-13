# 技术文档

本文件放**事实与细节**：架构、外部接口实测结论、算法、部署与调试流程。
**开发时要遵守的规则在 [`../CLAUDE.md`](../CLAUDE.md)**——那份要保持短，
新增的技术细节写到这里，别往那份里塞。

## 当前技术约束

每一条都是踩过坑之后定的，要改先说明为什么（CLAUDE.md 的规则）。来龙去脉见后面各节。

**边界**

- 不引入 Sonarr / Radarr / Bazarr：Bazarr 虽有 Jimaku provider（[PR #2505](https://github.com/morpheus65535/bazarr/pull/2505)），
  但强制依赖 *arr 提供媒体库信息，等于两套系统管同一个库。MoviePilot 已有识别结果和入库事件
- 与 Jellyfin 只有两处关系：按它的命名约定落盘、读它写在媒体库里的 nfo。不调用任何媒体服务器 API
- 手动页面是插件 API 直接返回的原生 HTML，不写 Vue 联邦插件页；不再拆独立服务（拆过又合回来了，见「架构」）
- 暂不引入 LLM：集号解析和 ID 映射都是确定性问题。唯一可能有价值的是映射表查不到时的标题消歧，
  若日后实测该路径误判率高，再作为可选 refiner 接在 `identify.search_anilist` 后面

**写盘**

- 落盘命名固定 `<视频文件 basename>.<语言后缀><扩展名>`，与视频同目录
- 覆盖已有字幕只有手动模式勾「覆盖已有」一个入口（`placer.place(overwrite=True)`）；自动入库路径永远不传
- 调试期三个安全阀：探针模式、dry-run、剧集白名单

**匹配**

- 主路径是 ID：`tmdb_id` → 离线映射表 → `anilist_id` → Jimaku。
  nfo 里只有 Bangumi ID 的剧：`bangumi_id` → BangumiExtLinker → MAL/AniDB → 映射表 → `anilist_id`
- 标题匹配只许全等：唯一放行的是 Bangumi 条目在映射表里没有外链时，用它的日文原名/别名
  与 Jimaku 条目名一字不差地比，唯一命中且开播年份相差不超过 1 年才算识别成功。
  Jimaku 的标题搜索是模糊的，绝不能取第一条；AniList 标题搜索的结果永远不算可信
- 剧级 Bangumi ID 只对应第一季：第 2 季往后必须有 season.nfo 或集 nfo 里的 ID
- 中文剧名不能拿去搜 AniList（实测时灵时不灵）。按中文名选剧一律走 `library.py` 读 `tvshow.nfo`
- nfo 里的 `<id>` 不是 tmdb：Jellyfin 往里写的是 TVDB ID。只认 `<tmdbid>` 和 `<uniqueid type="tmdb">`
- 身份不唯一就不下载：认不准是哪部剧、哪一集（映射表查不到、标题搜索命中、季号为 0）交人工
- 版本不唯一按规则挑：同一集同一语言档的候选依次按 同一份取 srt → 与视频同组 → 覆盖集数最多的组 →
  组内最近更新 → 组名。除第一步外必须按组算，保证整部剧落在同一个组

**字幕内容**

- 语言分档是主导项：片源偏好加分上限 19，小于分档最小间距 20
- 清洗只删行首整组标注和整行标注，行中间的括号不碰（`面白い（笑）` 是台词）
- 合并中日双语匹配率低于 50% 就不合，写纯日语
- 中文只认视频旁边的外置字幕文件；内嵌轨和硬字幕不处理（抽内嵌轨要 ffmpeg，容器里没法保证有）
- 合并后必须另存一份纯日语 `<视频名>.原文.ja.srt`
- 以上任何一步不确定，都退回"原样写入下载到的字幕"

**外部接口**

- Jimaku 认证 `Authorization: <token>`，不带 `Bearer ` 前缀
- Jimaku 限速 25 请求/分钟/Key，所有调用走统一节流器

**写代码时踩过的坑**

- `season` 不能用 `x or 1` 取默认值：季号 0 是合法值（特典），会被当成第 1 季。用 `if x is not None`
- 路径入参先判空再进 `pathlib`：`Path("")` 是 `WindowsPath('.')`，`.with_name()` 直接抛 `ValueError`
- `video_path` 是目录时不能直接 `.with_name()`，会算出目录的同级兄弟。一律先过 `placer.resolve_video()`
- 区分两种集号：`library_episode`（媒体库口径，找视频用）和 `Resolution.episode`（AniList 口径，匹配字幕用）。
  分季错位的番里两者不等，传错会给第 29 集配第 1 集的字幕
- 改完调试壳要重启；旧进程占着端口时新进程静默 bind 失败。`netstat -ano | grep :8990` 找 PID 再 `taskkill //F //PID <pid>`

## 字幕内容处理（为什么是现在这些规则）

### 语言分档与片源偏好

`picker.LANG_SCORES` 是主导项，片源偏好只用来在同档内打破平局。
分档最小间距 20 分，所以片源偏好的加分封顶 19，且只算优先级最高的那个命中关键词。
早期实现是 `(len(keywords) - rank) * 10`，最高加 40——足以把低一档的语言顶上来，
和"语言纯度是主导项"自相矛盾，写双语用例时被测出来，已修。

两档偏好（配置项「字幕偏好」，默认 `bilingual`）：

| | 顺序 |
|---|---|
| `bilingual`（默认） | 日中双语 > 纯日语 > 无语言标记 > 纯日语含 CC/SDH > 日英等 > 不含日语 |
| `japanese` | 纯日语 > 无语言标记 > 纯日语含 CC/SDH > 日中双语 > 日英等 > 不含日语 |

两张表只差 `ja_zh` 一项的位置，见 `picker.lang_scores()`。
2026-09-07 用户实测后改的默认值：纯日语要频繁停下来查词，影响观看。

CC/SDH 排在干净台词之后的原因：它是逐字听写、时间轴最准，但夹带说话人标注和音效描述。
标注现在会被 `cleaner.py` 清掉，但听写体和字幕组翻译体的差异清不掉，所以排序不变。

### 同分候选怎么挑（`picker._break_tie`）

早期规则是"分不出唯一最优就交人工"。2026-09-12 用真实 Jimaku 数据跑了三部剧：

| 剧 | 结果 | 同分的是什么 |
|---|---|---|
| 葬送的芙莉莲 第二季（entry 11446） | 10/10 集待人工 | NanakoRaws 同一份字幕的 `.ass` 和 `.srt` |
| 上伊那牡丹（entry 11837） | 12/12 集待人工 | Nekomoe kissaten&LoliHouse、Haruhana、KitaujiSub 三组的中日双语 |
| 葬送的芙莉莲（entry 729） | 1/28 集待人工 | 两组双语，外加一个被当成第 1 集的 `01-04` 合集文件 |

同分的候选都是这一集、同一语言档的字幕，挑哪个都不算下错；交人工只是把一个没有信息量的
选择推给用户逐集点。所以改成按规则挑：

1. **同一份字幕的不同格式**（去掉扩展名后同名）只留一个，按 `FORMAT_ORDER` 取，srt 优先——
   所有播放端都能直接显示，ass 在不支持的客户端上会让 Jellyfin 转码烧录
2. **与视频同组**：自动模式把下载时的原始文件名（`transferinfo.file_list` / `fileitem`）
   当 hint 传进来。入库会重命名，发布组只在原始文件名里还看得到
3. **覆盖集数最多的组** → 4. **组内最近更新** → 5. **组名排序**

3~5 是按组算的指标，每一集算出来都一样，所以整部剧各集会落在同一个组，
不会第 1 集 A 组、第 2 集 B 组。没写组名的文件（`Kinomi Master - 01 「…」 (MX …)`）
用"去掉分集标题和数字后剩下的部分"当组键，见 `picker.release_group()`。

顺带修的两个过滤问题：
- `BATCH_KEYWORDS` 里原来有 `season`，会把 `Show 2nd Season - 01.srt` 这种单集当合集丢掉
- 集号范围只写死了 `1-12` / `1-24`，`01-04` 漏网。改成正则 `_RANGE`，
  前面是连字符或数字的不算，避免日期 `2024-06-18` 被认成范围

仍然交人工的只剩**身份**问题：映射表查不到、只能靠标题搜索、季号为 0。

### 说话人标注清洗（`cleaner.py`）

实测同一集的五个不同来源都带 `（アイゼン）` 这类标注，**和文件名有没有标 `[cc]` 无关**，
所以清洗对所有下载的字幕统一做，不按语言分类区别对待。

- 删行首整组标注（`（）`/`【】`/`＜＞`，半角括号要求内含中日文字）
- 删整行只有标注、或只有音符（`♪〜`）的字幕条，删空的条目丢弃并重新编号
- **行中间的括号不碰**
- 支持 `.srt/.vtt/.ass/.ssa`；ASS 只动 Dialogue 的 Text 字段，`{\...}` 特效标签原样保留
- 一条也没删到就**返回原始字节**——因此即使编码猜错也不会把文件重写成乱码

### 中日双语合并（`merge.py`）

Jimaku 上现成的双语条目只有一部分番有，而中文字幕 MoviePilot 已经下在视频旁边了，两边一合就行。

- 以**日语字幕的时间轴为准**（它和这次的片源对得上），逐条去中文字幕里找重叠最多的一条
- 重叠时长要占较短那条的 30% 以上才算同一句；日语在上、中文在下
- 匹配率低于 `MIN_MATCH_RATE`（50%）判定为不是同一片源，**放弃合并**，写纯日语
- 中文字幕靠 `<视频名>.<语言标记>.<扩展名>` 里的语言标记识别；**没有语言标记的一律不用**，
  也不会把我们自己写的 `.ja.*` 当成中文来源。简繁都在时挑简体
- 合并输出统一是 `.srt`，目标文件名的扩展名会跟着变（原来是 `.ass` 也一样）

中文只认**视频旁边的外置中文字幕文件**。Jimaku 是日语字幕站，条目里的中文只以
「日中双语」文件的形式存在，没有独立的中文字幕可下；真出现双语文件时 `picker` 在默认偏好下
本来就会直接选它，轮不到合并。

**内嵌轨和硬字幕都不处理。** 曾经实现过用 `ffprobe`/`ffmpeg` 抽内嵌中文轨（v0.9.0 开发期），
当天就撤掉了：插件要跑在别人的 MoviePilot 容器里，没法保证 ffmpeg 存在，
为一个"可能有用"的来源加一条不可靠的外部命令依赖不划算。硬字幕烧在画面里，本来就没救。

### 另存一份纯日语

合并出来的双语是加工品，原件必须留着（用户明确要求：以备将来使用）。
另存为 `<视频名>.原文.ja.<原扩展名>`，常量在 `placer.RAW_TITLE`。

外挂字幕的命名约定是 `<视频名>.<标题>.<语言><扩展名>`，所以这个文件仍被
Jellyfin/Emby 识别成**日语轨**，只是在字幕菜单里显示为「原文」，和双语轨并列可选。

它不会干扰别的逻辑：`existing_ja_subtitle()` 找的是 `<视频名>.ja.` 前缀，匹配不到它；
`find_chinese_subtitle()` 只认中文语言标记，也不会把它当成中文来源。两条都有用例盯着。

### 覆盖已有字幕

只有手动页面能触发（`overwrite=True`），用途是把之前下的纯日语换成中日双语。
覆盖时如果旧文件扩展名和这次要写的不同（`.ja.ass` → `.ja.srt`），**必须删掉旧的**，
否则同一集会剩两份日语字幕，播放器挑哪份看不准，表现得像"改了没生效"。

## 架构：一个插件，装上即用

**全部代码跑在 MoviePilot 进程内的插件里，不需要额外的服务或容器。**

```
plugin/plugins.v2/jasubauto/
├── __init__.py     MoviePilot 集成层：事件监听 + get_api + get_form + get_page
└── core/           业务逻辑，不依赖任何 Web 框架
    ├── settings.py   普通 dataclass 配置（插件传 dict / 调试读 .env）
    ├── http.py       HTTP 垫片：有 httpx 用 httpx，否则用 requests
    ├── identify.py   tmdb_id → anilist_id + 集号换算
    ├── jimaku.py     Jimaku API 客户端（含限速）
    ├── picker.py     候选字幕排序与取舍
    ├── cleaner.py    写盘前清洗：去说话人标注/音效描述
    ├── merge.py      与中文字幕合成中日双语
    ├── placer.py     视频定位 + 落盘 + 覆盖保护
    ├── scan.py       单集处理 process_one + 整部剧批量补扫 scan
    ├── library.py    浏览媒体库、读 tvshow.nfo 取 tmdb_id
    └── ui.html       手动页面（原生 HTML+fetch，无前端框架）
```

**为什么手动页面能塞进插件**：MoviePilot 注册插件 API 的实现是 `router.add_api_route(**api)`，
整个 dict 直接展开成 FastAPI 参数，所以 `"response_class": HTMLResponse` 可以透传。
页面挂在 `/api/v1/plugin/JaSubAuto/ui`，用 `auth: "apikey"`（浏览器直接打开带不了 Bearer 头）。
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

## 已验证的外部接口事实（M0 实测，不要再猜）

### Bangumi ID（Jellyfin 的 Bangumi 插件刮削的库）

实测结论（2026-09-12）：

- **nfo 格式**：Jellyfin 的 nfo 保存器把 provider id 写成 `<{key 小写}id>`，插件的 key 是 `Bangumi`，
  所以是 `<bangumiid>`。三层都有：`SeasonProvider` 给每一季设自己的条目 ID（第一季回落到剧级 ID），
  `EpisodeProvider` 给每一集写 Bangumi 的**集 ID**
- **`<id>` 是 TVDB**：`SeriesNfoSaver` 把 TVDB ID 写进 `<id>`。早期 `read_nfo` 把 `<id>` 当 tmdb 兜底，已删
- **API**：`api.bgm.tv` 不带能认出应用的 UA 返回 403；条目不存在 404
  - `/v0/subjects/{id}`：`name` 是日文原名，`infobox` 的「别名」里常有和 AniList 罗马音一字不差的名字
  - `/v0/episodes?subject_id=&type=0`：正片列表，每集有 `sort`（跨季连续）和 `ep`（本季）。
    咒术回战第二季 `sort 25~47 / ep 1~23`，芙莉莲第二季 `sort 29~38 / ep 1~10`
  - `/v0/episodes/{id}`：带 `subject_id`、`sort`、`ep`，集 nfo 里的 ID 查这个
  - `/v0/subjects/{id}/subjects`：`relation == "续集"` 且 `type == 2`（动画）是下一季
- **BangumiExtLinker**（CC BY 4.0，自动匹配生成）：23206 个条目里 11085 个能经 MAL/AniDB 接到 AniList（48%）。
  **新番严重滞后**：芙莉莲第二季 2026-01 开播，9 月仍没有任何外链
- **Jimaku 标题搜索**（`/entries/search?query=`）是模糊的：`Kamiina Botan` 带出木の実マスター，
  `Dungeon Meshi` 带出三部《在地下城寻求邂逅》。日文名搜更准，但 Bangumi 的日文原名不一定等于
  Jimaku 的 `japanese_name`（咒术回战第二季：`呪術廻戦 懐玉・玉折／渋谷事変` vs `呪術廻戦 第2期`），
  别名里的罗马音 `Jujutsu Kaisen 2nd Season` 却和 Jimaku 的 `name` 一致。所以先用罗马音别名搜、
  再用日文名，只认 `name` / `japanese_name` / `english_name` 与标题集合全等的条目
- 同名重制版（如 1981 与 2022 的《うる星やつら》）靠 AniList 开播年份核对，差 1 年以上不认

识别顺序（`identify.resolve_bangumi`）：

1. 集 nfo 的集 ID → 条目 + 本季集号。**集号要和视频文件名对得上**（ep 或 sort 之一），
   对不上说明 nfo 过期，改用下一步
2. season.nfo 的条目；没有时用剧级 ID，但**只用于第 1 季**——TMDB 的第 2 季未必是 Bangumi 的下一个条目
3. `bangumi.locate` 把视频集号定位到条目内集号：本季编号直接用；连续编号查 sort；
   超出本条目集数就减掉集数、顺着续集往下走（芙莉莲 S01E36 → 第二季第 8 集）。
   走过续集后 sort 若也能算出结果，两者必须一致
4. 条目 → AniList：映射表（MAL/AniDB 指向多个 AniList 条目时交人工）→ 标题全等 + 年份核对

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
├── icons/jasubauto.png
├── plugins.v2/jasubauto/      # 项目主体，目录名 = 主类名小写
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
├── docs/TECH.md                   # 本文件：技术细节
├── LICENSE / README.md / CLAUDE.md
```

## 开发调试流程（按阶段推进，不要跳阶段）

核心原则：**把"能不能拿到字幕"和"能不能接上 MoviePilot"分开调**。80% 的代码不需要碰 MoviePilot。

- **M0 命令行跑通核心链路** ✅ 已完成（2026-09-06）：`python -m service.cli --tmdb-id 209867 --season 1 --episode 5`。识别、集号换算、候选筛选、偏好排序、落盘路径计算全部验证通过；实测结论见上面「已验证的外部接口事实」。
- **M1 手动网页** ✅ 已完成（2026-09-06）：脱机调试 `uvicorn service.main:app --port 8990`；生产是插件 API `/api/v1/plugin/JaSubAuto/ui`。两边同一份 `ui.html`。
- **M2 模拟入库事件** ✅ 已完成（2026-09-06）：`/api/jobs` 用模拟 payload 验证过（白名单、season=0、缺路径、非视频文件、已有字幕、跨季换算等边界）。
- **M3 触发器插件** 🔶 代码已写好（`plugin/plugins.v2/jasubauto/`），**默认开启「探针模式」只打日志不转发**。还没装到 MoviePilot 上验证过，`_build_payload()` 里的字段名属于待核对状态。装上去触发一次整理，从日志里核对字段，确认后关掉探针模式。不要对着文档猜字段。

  插件转发的 payload 是 `{tmdb_id, title, type, files: [视频路径...]}`——**故意不传季集号**，由服务侧用 `scan.parse_video()` 从文件名解析，和批量补扫共用同一套代码。这样插件只依赖 `tmdb_id` 和文件列表两个字段，MoviePilot 改 meta 语义也不会跟着坏。

  **装法（Synology Docker）**：把仓库整个拷到 NAS（如 `/volume3/docker/JaSubAuto/`），在 MoviePilot 容器上加挂载 `/volume3/docker/JaSubAuto:/plugins-dev`，加环境变量 `PLUGIN_LOCAL_REPO_PATHS=/plugins-dev` 和 `PLUGIN_AUTO_RELOAD=true`，重启容器 → 插件市场里能看到「日语字幕补全（Jimaku）」。插件配置里填 Jimaku Token 和番剧库目录 `/媒体/日番` 即可，没有服务地址/令牌这类东西。
- **M4 路径映射 + 存量补扫**：路径映射已确认不需要（见「部署环境」）。单文件夹批量补扫 ✅ 已完成（`core/scan.py` + `POST .../scan`）：递归找视频 → 解析季集 → 跳过已有 `.ja.*` 的 → 每个 AniList 条目只查一次 Jimaku 文件列表（限速）→ 逐集落盘。剩余：全库定时扫描。

### MoviePilot 侧调试操作（已查证）

- **装插件调试**：环境变量 `PLUGIN_LOCAL_REPO_PATHS`（本地插件仓库目录，多个用 `,` 分隔）+ `PLUGIN_AUTO_RELOAD=true`（热重载）。Docker 部署需把宿主目录挂进容器。
- ⚠️ `DEV=true` 也能热重载，但**会暂停所有定时任务**，不要在日常使用的实例上长期开启。
- **按需触发转移**（不用等新番）：历史记录 → 选中记录 → 重新整理。（是否会再次抛 `TransferComplete` 事件需要实测确认，这是装上插件后第一件要验证的事。）
- **看日志**：`LOG_LEVEL=DEBUG`；MoviePilot 后台实时日志页 / `docker logs -f` / `CONFIG_DIR`（Docker 通常 `/config`）下的日志文件。
- 如果 `PLUGIN_LOCAL_REPO_PATHS` 不生效，退路是把插件目录直接塞进容器的插件目录再重启（较笨但一定有效）。

### 网络：外部请求要走代理（2026-09-13 实测）

v0.7.1 部署后上伊那牡丹 12 集全部「未识别」，一次扫描超过 10 分钟。用 `MP_API_TOKEN` 调插件接口定位：

| 站 | 从 NAS 容器里 | 从局域网直连（绕过代理） |
|---|---|---|
| jimaku.cc | 正常（约 4s） | 很慢（约 20s） |
| graphql.anilist.co | 正常（约 4s） | 正常 |
| api.bgm.tv | **连不上** | **IPv4、IPv6 都超时** |

- 开发机开着 Clash（系统代理 `127.0.0.1:7890`），本地的"真实数据测试"全程走代理，所以没暴露问题
- MoviePilot 自带的 Bangumi 模块用 `RequestUtils(proxies=settings.PROXY)`；插件的 `http.py` 原先不走代理
- MoviePilot 的 `settings.PROXY` 是 `{"http": ..., "https": ...}`：优先 `PROXY_HOST`，没填时取
  `HTTPS_PROXY` / `HTTP_PROXY` 环境变量，都没有返回 `None`
- 失败原先被 `bangumi._api_get` 吞掉，每集每次调用都等满超时。现在连接超时 10s，
  某个站失败后 `http.DOWN_SECONDS`（5 分钟）内直接跳过，原因拼进识别说明

排查手段：手动页面「网络自检」，或 `GET /api/v1/plugin/JaSubAuto/netcheck?apikey=...`；
逐集的识别过程在扫描结果里展开，也写进 MoviePilot 日志。

### 发布：GitHub Release（2026-09-13 实测）

- **手动更新反而退回旧版本**：没声明 `release` 的插件，MoviePilot 先用 GitHub API 取文件清单，再逐个下载
  `download_url`（raw.githubusercontent.com 的固定地址，不带版本号）。下载**优先走「GitHub 加速」镜像站**
  （`settings.GITHUB_PROXY`），镜像站按地址缓存，于是 0.7.2 的更新装回了 0.7.0 的文件。
  插件索引刷新时带时间戳防缓存，下载文件时不带
- **改用 Release**：`package.v2.json` 标 `"release": true` 后，MoviePilot 按 tag `JaSubAuto_v<version>`
  找资产 `jasubauto_v<version>.zip`，经 GitHub API 下载，不走镜像站。Release 还没生成时自动退回文件清单安装
- 压缩包由 `.github/workflows/release.yml` 在 `package.v2.json` 变化时打包：文件放在包的根目录；
  version 与 `plugin_version` 不一致直接失败；同一个 tag 已存在就跳过，所以**每次发布必须 bump 版本号**
- **图标**：前端对 `http` 开头的图标经后端图片代理加载，否则去它自带的 `plugin_icon/` 目录找——那里只有官方插件的图标。
  所以插件类的 `plugin_icon` 和 `package.v2.json` 的 `icon` 都写完整的 raw 地址
  （`raw.githubusercontent.com` 在 MoviePilot 默认的图片域名白名单 `SECURITY_IMAGE_DOMAINS` 里）

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

# 批量补扫一整部剧（默认 dry-run，加 --write 才真写，--overwrite 连已有字幕也重下）
python -m core.scan --dir "/媒体/动漫/某剧" --tmdb-id 209867

# 跑测试
pytest tests/
```

## 手动页面的交互原则

**是「搜索」不是「浏览」。** 番剧库动辄几百部，一进页面就把整库铺出来没法用，加筛选框也只是治标。
所以 `library.find_shows()` 在关键字为空时**只报总数、不返回任何剧**，输了关键字才列匹配项。
统计集数要递归扫目录，也只对匹配到的（≤30 部）做。手工指定目录的 `browse()` 收进折叠区兜底。
