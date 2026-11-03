# 监控调度

监控分成两个 systemd oneshot 服务，分别使用自己的状态文件，避免资源采样被 QQ/HTTP 探测阻塞，也避免并行写坏状态。

- `ygo-health-resources.timer`：每轮结束后 30 秒再次采样。默认可用内存 <150MiB 或根分区使用率 >80%，连续异常至少 120 秒才通知；恢复到阈值内才发送恢复通知。没有达到持续时间的短暂波动不发送异常或恢复邮件。超过 90 秒没有有效采样、或系统重启，重新计算持续时间。
- `ygo-health-heartbeat.timer`：每轮结束后 60 秒再次检查服务、端口、HTTP、OneBot 和指标文件心跳。异常/恢复按状态转换通知；原有服务自动恢复行为保留。探测本身有耗时，因此不是硬实时周期。
- 同一持续异常每 1800 秒提醒一次；恢复后再次异常不受上一次通知冷却限制。
- 资源状态使用固定标识，数值仅作详情。资源恢复邮件附本次内存/磁盘读数，不再把旧的 84MB 当成恢复读数。

配置写入 `/etc/ygobot/notify.env`（参见 `notify.env.example`）：
`HEALTH_MIN_AVAILABLE_MB`、`HEALTH_MAX_DISK_PERCENT`、`HEALTH_RESOURCE_HOLD_SECONDS`、`HEALTH_REPEAT_SECONDS`。
改变采样频率需调整相应 timer；若资源采样间隔超过 90 秒，还需同步调整代码的连续采样最大间隔。

安装四个 `ygo-health-*.service/timer` 到 `/etc/systemd/system/`，执行 `systemctl daemon-reload`，然后 `systemctl enable --now ygo-health-heartbeat.timer ygo-health-resources.timer`。
移除旧的 cron healthcheck 行，保留备份任务。`deploy/ygobot.cron` 已不包含旧监控任务。

只读检查：
```
python3 /opt/ygobot/tools/healthcheck.py --resources --dry-run
python3 /opt/ygobot/tools/healthcheck.py dry-run
systemctl list-timers 'ygo-health-*'
```

这仍是服务器内部监控：整台服务器冻结时，无法保证发出通知。要发现整机失联，需要服务器外部的可用性探测或云监控。
