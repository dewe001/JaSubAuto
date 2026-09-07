# JaSubAuto

MoviePilot 插件：为番剧媒体库自动补日语字幕，字幕源 [jimaku.cc](https://jimaku.cc)。

MoviePilot 自带的字幕插件只覆盖中文字幕站点，本插件补上日语字幕这一环。
单个 V2 插件，不依赖额外服务、容器或 Sonarr/Radarr。

## 功能

- **自动**：入库完成后为新剧集下载日语字幕，处理放在后台线程，不阻塞入库流程
- **手动**：插件自带网页，搜剧 → 批量补扫整季 → 试运行确认 → 写入。已有日语字幕的集数默认跳过，
  勾「覆盖已有」则重下替换（用来把旧的纯日语换成双语）
- **不猜**：识别不可信、候选分不出优劣、季号为 0 的特典，一律标记待人工；已存在的字幕永不覆盖

匹配全程走 ID（`tmdb_id` → 离线映射表 → `anilist_id`），能正确处理 TMDB 与 AniList
的分季集号错位——例如媒体库里的 S01E37 对应 AniList 第二季第 9 集。

## 安装

计划提交至 MoviePilot 官方插件市场，届时可直接搜索安装。在此之前用本地目录方式：

```yaml
volumes:
  - /path/to/JaSubAuto:/plugins-dev
environment:
  - PLUGIN_LOCAL_REPO_PATHS=/plugins-dev
  - PLUGIN_AUTO_RELOAD=true
```

重启后在插件市场安装「日语字幕补全（Jimaku）」。

## 配置

| 项 | 说明 |
|---|---|
| Jimaku API Token | 在 <https://jimaku.cc/profile> 申请 |
| 番剧库目录 | 填到番剧那一层，如 `/媒体/日番`；填媒体库总根目录会把下载目录一并扫入 |
| 字幕偏好 | 中日双语优先（默认）/ 纯日语优先 |
| 去说话人标注 | 默认开启，见下 |
| 合成中日双语 | 默认开启，见下 |
| 另存纯日语 | 默认开启 |

三个安全阀默认开启，建议逐个关闭：**探针模式**（只打日志不处理）、**试运行**（只显示
将写入的路径不落盘）、**剧集白名单**（填 `tmdb_id`，留空为不限制，仅约束自动模式）。

## 落盘命名

```
葬送的芙莉莲 - S01E36 - 第 36 集.mkv
葬送的芙莉莲 - S01E36 - 第 36 集.ja.srt        ← 本插件写入
葬送的芙莉莲 - S01E36 - 第 36 集.zh-Hans.srt   ← MoviePilot 原有中文字幕，不受影响
```

「视频同名 + `.ja` + 字幕原扩展名」，写在视频同目录，Jellyfin / Emby / Plex 通用。
本插件不与媒体服务器通信，写入后等扫库即可。

## 字幕优先级

同一集通常有十余个候选，排序以语言构成为主、片源偏好为辅。默认（中日双语优先）：

日中双语 > 纯日语 > 无语言标记 > 纯日语含 CC/SDH 注释 > 日英等双语 > 不含日语

面向日语学习，双语字幕免去逐词查典；切换为「纯日语优先」则把日中双语降到日英之前、
CC/SDH 之后。片源偏好只在语言构成相同时用来打破平局，加分有上限，跨不过语言档。
仍分不出唯一最优则交人工。调整权重改 `core/picker.py` 的 `LANG_SCORES`。

## 去说话人标注

日语听障字幕（CC/SDH）时间轴最准，但每行前面挂着 `（フリーレン）` 这类说话人名，
还夹带 `（ドアが開く音）` 和 `♪〜`。实测这类标注在各来源中普遍存在，不限于文件名标了
`[cc]` 的那些，因此 `core/cleaner.py` 在写盘前统一清洗（可在配置里关闭）：

- 删行首的整组标注，以及整行只有标注或只有音符的字幕条（删空的条目会被丢弃并重新编号）
- 行中间的括号一律不碰（`面白い（笑）` 是台词的一部分）
- 支持 `.srt` / `.vtt` / `.ass` / `.ssa`；ASS 只动 Dialogue 的 Text 字段，特效标签原样保留
- 没删到任何东西就原样写回原始字节，编码猜错也不会产出乱码

## 合成中日双语

Jimaku 上现成的双语字幕只有一部分番有。视频旁边若已有中文字幕（MoviePilot 下的），
`core/merge.py` 会以**日语的时间轴**为准，逐条找时间上重叠最多的中文接在下面，输出一份双语 `.srt`：

```
そうだね
是啊
```

匹配率低于 50% 判定为两份字幕不是同一个片源，放弃合并、只写纯日语——宁可单语也不错位。

中文只认视频旁边的外置字幕文件，靠文件名语言标记识别（`.zh-Hans` / `.chs` / `.繁体` 等），
没有标记的一律不用。旁边没有中文字幕就照常写纯日语——内嵌在视频里的和硬字幕都不处理。

合并成功时会另存一份纯日语 `<视频名>.原文.ja.srt`。外挂字幕的命名是
`<视频名>.<标题>.<语言><扩展名>`，所以它仍被识别为日语轨，在播放器字幕菜单里显示为「原文」，
和双语轨并列可选。

## 限制

- 仅处理日语字幕；不做翻译、时间轴校正、字幕烧录
- 全库定时补扫尚未实现，目前按剧手动触发
- 映射表中同时含 `anilist_id` 与 `themoviedb_id` 的仅 6837 条，查不到属常态，手动模式为必需功能
- 目标为 MoviePilot V2

## 开发

`service/` 是脱机调试壳，导入同一份 `core/`，用于不启动 MoviePilot 时调试：

```bash
pip install -r requirements.txt
cp .env.example .env                       # 填 JIMAKU_API_TOKEN 与 MEDIA_ROOTS
uvicorn service.main:app --port 8990       # 页面与插件内的完全一致

python -m pytest tests/                    # 51 项
node tests/test_ui.mjs                     # 11 项，实际执行 ui.html 中的脚本
```

架构、实测结论与踩过的坑见 [`docs/TECH.md`](./docs/TECH.md)；开发时要守的规矩见
[`CLAUDE.md`](./CLAUDE.md)。

## 参考

- [Jimaku](https://jimaku.cc) · [API 文档](https://jimaku.cc/api/docs)
- [`Fribb/anime-lists`](https://github.com/Fribb/anime-lists)：ID 映射表，首次运行自动下载
- [MoviePilot V2 插件开发指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/V2_Plugin_Development.md)

## License

MIT
