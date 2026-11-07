# 保密命令收紧 + 慢命令延迟提示 + 周报巡检（2026-11-07 上线）

## 1. 背景

用户反馈两件事：

1. 群里不 @ 也能触发 `开通查询`、`令牌分发`，且名单外会收到“只对保密名单开放”的提示——等于暴露了功能存在；
2. 帮助清单里，名单外用户仍能看到 `令牌分发`（以及统计类命令），与“保密”预期不符。

同时按需加入“慢命令延迟提示”，减少一次查询拆成两条消息的情况。

## 2. 行为变化

### 2.1 保密命令

| 场景 | 之前 | 现在 |
|---|---|---|
| 群内裸发 `开通查询` / `令牌分发` | 触发（名单外回拒绝语） | **无任何反应** |
| 群内 @ 后发，名单外 | 回“只对保密名单开放” | **无任何反应** |
| 私聊发，名单外 | 回“只对保密名单开放” | **无任何反应** |
| 名单外 `帮助` | 显示统计类与「令牌分发」 | **不显示** `统计`、`统计对局`、`观战设备`、`开通查询`、`令牌分发` |
| 名单内 | 不变 | 不变 |

判定统一走 `watch_token_command.confidential_allowed`：优先 `remote.token_groups/token_users`，未配置时回退
`statistics.allowed_groups/private_users`（当前即回退，2 群 / 3 人）。

### 2.2 慢命令延迟提示

原来先发“处理中”再发结果（2 条）。现在：

- 结果在 3 秒内完成 → **只发结果（1 条）**；
- 超过 3 秒 → 先补一句“处理中”，再发结果（2 条，但用户确实需要）。

覆盖四处：`deck_build`、`player_history`、`price_quote`、`SLOW_COMMAND_HINTS`（stats_page/card_search 等）。
实现为独立的 hint watcher 任务，不阻塞指令分发，也不拉长 `command_complete` 统计。

### 2.3 周报巡检

`ygobot-qq-weekly.timer`，每周一 09:00 运行 `/opt/ygobot/tools/qq_weekly_report.py`，向
`data/logs/qq-weekly-report.log` 追加一行：

```
2026-11-07 11:10 近7天：账号离线 2 段/55.2 分钟；接口断连 10 段/119.0 分钟；发送 1177；命令 762；接口错误 20；离线时间：11-04 02:25、11-06 11:37
```

只读诊断文件，不连网、不发消息。

## 3. 文件与摘要

| 文件 | 说明 | sha256 前 8 |
|---|---|---|
| `bot/qq.py` | 免@路由排除 `query_enroll`/`watch_token` | `04481eaf` |
| `bot/service.py` | 帮助过滤、名单外静默、延迟提示 | `6ba03f48` |
| `tools/qq_weekly_report.py` | 周报脚本 | 随文件 |
| `tools/qq_restart_waiter.py` | 静默 5 分钟后重启的等待器 | 随文件 |
| `deploy/…`（服务器 `/etc/systemd/system`） | `ygobot-qq-weekly.service/.timer` | — |

备份：`/opt/ygobot/bot/qq.py.bak-20261107-conf`、`/opt/ygobot/bot/service.py.bak-20261107-conf`。

## 4. 部署与重启

- 2026-11-07 11:10 文件已上传并 `py_compile` 通过；**未立即重启**。
- 按用户要求：连续 300 秒无 `command_start`/`reply_queued`/`reply_complete` 后自动重启一次，
  等待器为 `tools/qq_restart_waiter.py`，结果写 `data/logs/qq-restart-waiter.json`。
- 注意：`service.py` 此前还有另一会话的「每日一卡 / 概率运算」待重启改动（`97a6af02` 基线），
  **本次重启会一并激活**。

## 5. 验证

部署后人工核对：

1. 名单外私聊发 `令牌分发`、`开通查询` → 无回复；
2. 名单外群裸发这两个词 → 无回复；@ 后发 → 无回复；
3. 名单外 `帮助` → 无 `统计`/`统计对局`/`观战设备`/`令牌分发`/`开通查询`；
4. 名单内 `帮助` 与命令行为不变；
5. 快命令（如 `查卡`）只回一条；慢命令（`构筑`/`历史`）超过 3 秒才有“处理中”。

## 6. 回滚

```bash
cp -a /opt/ygobot/bot/qq.py.bak-20261107-conf /opt/ygobot/bot/qq.py
cp -a /opt/ygobot/bot/service.py.bak-20261107-conf /opt/ygobot/bot/service.py
chown ygo:ygo /opt/ygobot/bot/qq.py /opt/ygobot/bot/service.py
python3 -m py_compile /opt/ygobot/bot/qq.py /opt/ygobot/bot/service.py
systemctl restart ygo-qq
```

周报：`systemctl disable --now ygobot-qq-weekly.timer`。

## 7. 未完成 / 风险

- `bot/service.py` 本轮未提交 GitHub：该文件包含另一会话尚未提交的 `daily_card`/`probability` 接线，
  直接提交会引用仓库里不存在的模块。待对方连同新模块一起提交后，本轮的 service 改动会一并进入。
- `bot/qq.py` 已提交（仅含本轮免@改动）。
- 帮助过滤改为只看 `confidential_allowed`；当前名单与统计名单同源，若以后拆分配置，需要重新确认统计命令的可见性。
