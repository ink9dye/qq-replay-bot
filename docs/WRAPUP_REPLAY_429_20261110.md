# 录像批量下载 429 收尾（2026-11-10）

## 1. 现象

PC 客户端 v1.6「录像库 → 下载全部（当前筛选）」弹窗：

```
有 33 场没下载成功：
20261105-0642_fakefakefake_轮回虫龙_vs_qwqwaw_...
（请求太频繁，稍后再试）
```

失败原因统一是「请求太频繁，稍后再试」，对应 HTTP 429；单场下载本身没有坏，重试即可成功。

## 2. 根因

录像库的公网入口是 8787 的 nginx：

```nginx
location ^~ /replay-api/ {
    limit_req zone=api burst=20 nodelay;   # 旧：5r/s，burst 20，与统计 API 共用
    ...
}
```

`zone=api` 的速率是 **5 请求/秒**、突发 20。而「下载全部」是**逐场一个 zip 请求**，单场 zip 只有几 KB、服务端几十毫秒就返回，客户端连续发请求时很容易超过 5 次/秒；超过 burst 的部分被 nginx 直接 429，不会到达 `ygo-replay-api`，所以服务端日志里看不到这些失败。

客户端 v1.6 只有失败计数、没有 429 重试，于是整批里被限流的那部分被记成“没下载成功”。

## 3. 修复

### 3.1 服务端（nginx，已生效）

- 新增独立 zone：`limit_req_zone $binary_remote_addr zone=replay:10m rate=20r/s;`
- `/replay-api/` 改为 `limit_req zone=replay burst=60 nodelay;`
- `nginx -t` 通过后 `systemctl reload nginx`（平滑加载，**没有重启任何 systemd 服务**，不影响录制）。
- 备份：`/root/nginx-backups/ygobot.bak-replay-rate-20261110`。

带宽总闸没动：`ygo-replay-api` 里所有下载仍共享 200 KiB/s 令牌桶、并发 4；这次只是把“请求次数”的限制放到合理范围。

### 3.2 客户端 v1.6.1

- `download_match` 对 429/503 自动重试 3 次，退避 0.6 / 1.2 / 1.8 秒；其它错误不重试。
- 下载循环每场后 `sleep(0.05)` 轻微节流，避免整批下载瞬间打满请求速率。
- 版本号 1.6.1，窗口标题可区分。

## 4. 验证

- 服务端：同一令牌对 `/replay-api/matches` 连发 50 次 → **200 × 50，4.54 秒，无 429**（改动前同样打法会在第 21 次左右开始 429）。
- 客户端：24 项单测通过；`MoeCardWatch.exe --smoke-test` → `SMOKE_OK`。
- 线上服务：`nginx` / `ygo-replay-api` / `ygo-collector` / `ygo-web` 均 active；8787、8788 `/health` 200；`/replay-api/ping` 无令牌 401（鉴权正常）。

## 5. 部署与回滚

- 已部署：nginx 配置（reload）+ 客户端 v1.6.1 包；`bot/replay_api.py` 本轮**无改动**，服务未重启。
- 回滚 nginx：

```bash
cp /root/nginx-backups/ygobot.bak-replay-rate-20261110 /etc/nginx/sites-enabled/ygobot
nginx -t && systemctl reload nginx
```

- 客户端回滚：继续用 v1.6 或更早版本即可（服务端限流放宽后，旧客户端顺序下载通常也不再触发 429，只是没有自动重试）。

## 6. 影响面与边界

- 只影响 `/replay-api/` 的请求速率限制；`/stats`、`/s`、`/watch-api` 的限流不变。
- 带宽仍由服务端 200 KiB/s 全局限速兜底，放宽请求数不会让 3 Mbps 出口被占满。
- 429 仍是保护：异常高频请求（例如脚本并发刷）依旧会被 nginx 拦下。
- 若未来多个客户端同时整批下载，仍可能触发 20r/s；客户端已有重试，且可再把 burst 调大，但需同时评估 3 Mbps 带宽。

## 7. 文件与提交

| 项 | 值 |
|---|---|
| nginx | `/etc/nginx/sites-enabled/ygobot`（新增 `zone=replay`，`/replay-api/` 改用） |
| 客户端 | `clients/MoeCardWatch/app/replay_api_client.py`、`app/replay_browser.py`、`paths.py`、`version_info.txt` |
| 交付包 | `exports/MoeCardWatch-v1.6.1-win64.zip`（13.2 MB，sha256 `9C26C416…1335C72`） |
| 接口文档 | `docs/QUERY_API.md` 7.7 / 7.8 已改为 `zone=replay` |
| GitHub | `b46e256`（docs，rebase 到 `8d4bb84` 后推送） |
| 交接 | `PROJECT_HANDOFF.md` 顶部 2026-11-10 13:00 条 |
