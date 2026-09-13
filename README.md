# JaSubAuto

MoviePilot V2 插件：为番剧媒体库自动补日语字幕，字幕源 [jimaku.cc](https://jimaku.cc)。

## 功能

- 入库后自动下载日语字幕，写成 `<视频名>.ja.srt`，Jellyfin / Emby / Plex 直接识别
- 手动页面：搜剧 → 整季补扫 → 试运行确认 → 写入；可勾「覆盖已有」替换旧字幕
- 默认中日双语优先；视频旁已有中文字幕时自动合成双语，并另存一份纯日语（字幕菜单里显示为「原文」）
- 去掉字幕里的说话人标注和音效描述
- 支持 nfo 里只有 Bangumi ID 的剧（Jellyfin 的 Bangumi 插件刮削，需开启 nfo 保存）
- 认不准是哪部剧、哪一集时不下载，标记待人工

## 安装

计划提交至 MoviePilot 官方插件市场。在此之前用本地目录方式：

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
| 番剧库目录 | 填到番剧那一层，如 `/媒体/日番` |
| 字幕偏好 | 中日双语优先（默认）/ 纯日语优先 |
| 去说话人标注 | 默认开启 |
| 合成中日双语 | 默认开启 |
| 另存纯日语 | 默认开启 |
| 代理 | 留空跟随 MoviePilot 的代理设置。国内访问 Bangumi 需要代理 |

首次使用建议保持**探针模式**和**试运行**开启，确认日志无误后再关闭；
**剧集白名单**（填 `tmdb_id`）可把自动模式限制在一两部剧上。

## 限制

- 只做日语字幕，不做翻译、时间轴校正
- 全库定时补扫尚未实现，目前按剧手动触发
- 映射表覆盖不全，部分剧需要在手动页面指定

## 开发

```bash
python -m pytest tests/
node tests/test_ui.mjs
```

技术文档见 [`docs/TECH.md`](./docs/TECH.md)，开发规则见 [`CLAUDE.md`](./CLAUDE.md)。

## 数据来源

[Fribb/anime-lists](https://github.com/Fribb/anime-lists) ·
[BangumiExtLinker](https://github.com/Rhilip/BangumiExtLinker)（CC BY 4.0） ·
[Bangumi API](https://bangumi.github.io/api/)

## License

MIT
