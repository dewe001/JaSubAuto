# CLAUDE.md

本文件只放**开发时必须遵守的工作规则**，要短到能一口气读完。

技术路线、当前的匹配与字幕处理约束、接口实测结论、踩过的坑，都在 [`docs/TECH.md`](./docs/TECH.md)。
那些会随方案调整，调整时改那份，不改本文件。

## 项目

JaSubAuto：MoviePilot V2 插件，给番剧媒体库补日语字幕，字幕源 jimaku.cc。单人自用。

## 动手之前

- **范围**：只做日语字幕的获取与落盘。不做翻译、时间轴校正、中文字幕、多用户；
  不引入新的外部服务或依赖（Sonarr/Bazarr、ffmpeg、LLM 等），确有需要先问用户
- **改 `docs/TECH.md`「当前技术约束」里的任何一条，先说明理由**。每条都是踩坑定的，不许顺手改
- 用户说「先分析」时只分析，不改代码

## 安全底线（不随技术路线变）

- 默认不覆盖已有字幕；所有写盘支持 dry-run，且默认 dry-run
- 认不准就不下载，交人工。宁可漏，不可错
- 失败静默降级：外部接口报错、超时只记录，不抛异常影响 MoviePilot
- 密钥只放 `.env`（已 gitignore），不进代码、配置默认值和仓库
- 单测不联网，外部调用一律 mock

## 排查与验证

- **日志要能回答"这一集为什么没下到字幕"**：每次外部请求的结果（含报错原因）和每一步判断都要记录，
  用户在手动页面上能直接看到。吞掉异常却不留原因，用户就没法排查
- **交给用户测之前，先在 NAS 上验证**：用 `.env` 里的 `MP_API_TOKEN` 调插件接口，确认真实环境跑通。
  本地通过不代表容器里通过——网络、代理、数据都不一样，已经因此让用户白测过一次
- 访问 NAS 只做只读接口和 dry-run；任何写操作先问用户

## 版本控制

**未经批准不许 `git commit` / `git push`**，包括 `--amend`、`push -f`、`git tag`，以及开 PR、合并 PR、
发 release、改仓库设置等一切对外可见的操作。一次批准只管一次。

**不在 `main` 上开发，不在本地合并分支**，走 GitHub flow：

```bash
git switch main && git pull              # 1. 从最新的 main 出发
git switch -c <type>/<简短英文描述>       # 2. 开分支（type：feat/fix/docs/refactor/chore）
#    ... 开发、提交 ...
git push -u origin <分支名>               # 3. 推分支（要批准）
#    4. 在 GitHub 上开 PR 并合并（要批准）
git switch main && git pull              # 5. 拉回合并结果，回到第 2 步
```

- 本地 `main` 只用来 pull。误提交在 `main` 上：`git switch -c <分支名>` 保住提交，
  再 `git branch -f main origin/main`
- 一个分支一件事，合掉就删
- **不同功能分别提交**，顺手修的 bug 也单独提交；同一个分支里照样按功能拆成多个提交
- **提交信息只简要描述本次改动**：一行 `<type>: <简述>`，最多补一两句为什么。不写实现细节、验证过程、测试数量

## 交付（让用户测之前必须做完）

1. **跑测试**：`python -m pytest tests/`；改了 `ui.html` 再跑 `node tests/test_ui.mjs`
2. **在 NAS 上验证**（见上）
3. **bump 版本号**：`plugin_version` 与 `package.v2.json` 的 `version` 同时改，`history` 写清改了什么。
   不 bump 的话 MoviePilot 认不出新版本，用户测到的可能是旧代码
   - 版本号对应一次发布：上次推送后只 bump 一次，之后的改动补进同一条 `history`
   - 默认只 bump 补丁号（`0.7.0` → `0.7.1`）；升次版本号由用户决定；真机验证通过前不上 `1.0`
4. 部署后先看页面右上角的版本戳，确认加载的是哪一版，再判断功能对不对
5. **README 只写用户视角**：能做什么、怎么装、怎么配、有什么限制。实现细节一律写 `docs/TECH.md`

## 前端

内联 `onclick` 里不许拼模板变量，用 `data-*` 属性（值过 `esc()`）+ 事件委托。`tests/test_ui.mjs` 有 lint 挡着。
