# AGENTS.md — 协作者须知（GitHub）

本文件面向 **GitHub 协作者** 与接手本仓库的 AI 对话。先读它和 [`docs/INDEX.md`](docs/INDEX.md)，再按需深入。

## 1. 项目速览

YGOPro/萌卡观战采集 + 统计网站 + QQ 机器人。生产运行在阿里云 2C2G：

- 线上代码目录：`/opt/ygobot`（**手工 scp 部署，不是 git 仓库**）；
- 服务：`ygo-collector`、`ygo-web`（统计后台 8788）、`ygo-qq`、`ygo-replay-api`（8792），NapCat 在 Docker；
- 公网入口：`http://<服务器>:8787`（nginx → 分享本体 8789）。

## 2. 接手 / 阅读顺序

1. [`docs/INDEX.md`](docs/INDEX.md) —— 文档总索引；
2. 工作区 `readme.md`（定位、当前状态、常用命令）；
3. `PROJECT_HANDOFF.md` 顶部摘要 + 最新条目（线上状态、最近改动、遗留）；
4. 核心：`ARCHITECTURE` → `OPERATIONS` → `DEPLOY` → `FEATURES` → `KNOWN_ISSUES`；
5. 规则/冻结：`LOGIC_FREEZE.md`（取源、卡图、构筑推断、Side、命名算法等冻结项）。

> 本仓库的 `docs/` 是线上文档的子集；完整运维文档在工作区。不要把历史报告当作当前状态。

## 3. 开发与测试

- Python 3.12，标准库优先；新增依赖前先说明必要性；
- 复用现有缓存/模块；不擅自新建中间层表或框架（需先讨论）；
- 改完运行相关测试；至少 `py_compile`；
- 前端改动需真实浏览器验收（含移动端）；静态资源有 1 小时缓存，验收前 Ctrl+F5。

## 4. 分支、提交与交付

- 分支：`delivery/<主题>-<日期>`；不经确认不要直接推 `master`；
- 提交：一次一个主题，`type(scope): 摘要`（`fix` / `perf` / `docs` / `chore`）；
- 交付说明/PR 必须包含：背景、改动、验证（命令与结果）、回滚、影响面（服务/接口/UI）；
- 交付分支中与生产相关的文件，提交前核对其 `sha256` 与线上一致。

## 5. 安全与红线

- **禁止提交**：`data/`、`config.json`、数据库、日志、备份目录、令牌/密钥、`.tmp-ops/` 临时产物；
- 不在代码、文档、提交信息里粘贴账号凭据；
- 不直接在生产上做未备份的修改；生产重启需维护者同意，且避开正在录制对局；
- QQ 账号处于观察期时，按 `docs/QQ_OBSERVATION_PERIOD_RULES.md` 执行（禁止登录类操作）。

## 6. 文档要求

- 新报告放 `docs/`，文件名带日期后缀（`_YYYYMMDD`）；一次性证据放 `workflow-results/<主题>-<日期>/`；
- 改完代码/配置：更新 `PROJECT_HANDOFF.md` 顶部摘要 + 最新条目；
- 新增文档：到 `docs/INDEX.md` 加一行；同一事实只保留一个“最新版本”。

## 7. 目录约定

| 路径 | 说明 |
|---|---|
| `bot/` | 服务代码（web 入口、路由缓存、统计口径、饼图、QQ 等） |
| `docs/` | 长期文档（入口 `docs/INDEX.md`） |
| `deploy/` | systemd/nginx 配置样例 |
| `stability/` | 稳定性候选（未部署） |
| `tools/` | 运维/验证脚本 |
| `workflow-results/` | 一次性验证产物（不入库或不长期保留） |
