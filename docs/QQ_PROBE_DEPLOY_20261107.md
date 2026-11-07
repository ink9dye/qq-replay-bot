# QQ 转人工后自动探测恢复（2026-11-07 上线收尾）

## 1. 背景

2026-11-04 02:25 账号离线，`ygo-qq` 连续重连 150 次（约 25 分钟）后在 03:03 转 `manual_attention`
并 `break`，进程停泊；直到 07:35 才有新进程恢复。中间即使账号恢复，机器人也不会自己连回来。

同类风险：凌晨掉线 → 无人处理 → 整个上午机器人不可用。

## 2. 变更

文件：`bot/qq.py`（唯一改动，+约 80 行）

| 项 | 内容 |
|---|---|
| 新参数 | `OneBotClient(..., probe_seconds=None)` |
| 配置 | `QQ_AUTO_RESUME_PROBE_SECONDS`（默认 0=关闭） |
| 上限 | `QQ_AUTO_RESUME_PROBE_MAX_HOURS`（默认 24，0=不限） |
| 触发点 | 连续失败 150 次、`pause_for_attention` 之后 |
| 探测内容 | `get_login_info` + `get_status`，双确认才恢复 |
| 禁止项 | 不登录、不扫码、不重启 NapCat、不发消息 |
| 停止条件 | 账号受限提示（`restriction_hint`）/ 账号不一致 / 满 24 小时 / 进程停止 |
| 默认行为 | 与旧版完全一致（关闭时不探测，直接 break） |

实现要点：
- 探测期间不依赖主循环 `_read_loop`，用 `_probe_exchange()` 直接收发；
- 等待按 5 秒切片，`stop()` 后最多 5 秒退出；
- 恢复时调用 `recovery.stable()`，会补发一次“已恢复在线”提醒；
- 超时写入停泊原因并记录 `auto_resume_probe_expired`。

## 3. 部署记录

| 项 | 值 |
|---|---|
| 部署时间 | 2026-11-07 09:04:16 |
| 生产文件 | `/opt/ygobot/bot/qq.py`，sha256 `06169c23…`（基线 `f193f99b…`） |
| 备份 | `/opt/ygobot/bot/qq.py.bak-20261107-0904` |
| systemd drop-in | `/etc/systemd/system/ygo-qq.service.d/probe.conf` |
| 内容 | `QQ_AUTO_RESUME_PROBE_SECONDS=300`、`QQ_AUTO_RESUME_PROBE_MAX_HOURS=24` |
| 重启范围 | 仅 `ygo-qq`；未重启 NapCat、未登录 QQ、未改账号 |

## 4. 验证

```
systemctl is-active ygo-qq          -> active（NRestarts=0）
qq-status.json                      -> state=online, pid=673438, expected_self_id=3528698561
/health                             -> 200
qq-diagnostics.jsonl                -> runtime_start -> connect_attempt -> connection_state=online -> heartbeat success
journalctl -u ygo-qq                -> 无 Traceback
/proc/<pid>/environ                 -> 两个 QQ_AUTO_RESUME_* 变量存在
```

测试：`py -3 -m unittest discover -s tests -t .` → 12 项通过（本地候选包）。

## 5. 上线后如何观测

真实转人工后，`data/logs/qq-diagnostics.jsonl` 会出现：

| 事件 | 含义 |
|---|---|
| `auto_resume_probe_start` | 开始探测（`retry_seconds` 为间隔） |
| `auto_resume_probe` | 每次探测结果（`success` 是否在线；失败带 `error_type`/`reason`） |
| `auto_resume_probe_expired` | 满 24 小时停止探测 |

恢复成功后 `qq-status.json` 回到 `online`，无需 `resume-qq` 或重启。

## 6. 回滚

```bash
cp -a /opt/ygobot/bot/qq.py.bak-20261107-0904 /opt/ygobot/bot/qq.py
chown ygo:ygo /opt/ygobot/bot/qq.py
python3 -m py_compile /opt/ygobot/bot/qq.py
systemctl restart ygo-qq
```

回滚后摘要应恢复 `f193f99b…`。drop-in 可保留，旧代码不读取这两个变量。

## 7. 未做 / 后续

- 退避候选（`stability/qq-backoff-20261106`，150→90 次、后段 20/30 秒间隔）**未部署**，待单独评估。
- 尚未在真实转人工场景中观察到一次自愈；下次掉线超过 25 分钟时按第 5 节核对。
- 探测只覆盖“转人工之后”；短故障仍走原有重连逻辑，行为未变。
