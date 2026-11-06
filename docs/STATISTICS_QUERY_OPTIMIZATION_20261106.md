# 统计查询耗时优化（2026-11-06 封版）

本文件记录本轮“卡片统计和录像筛选速度优化”的最终交付：Side 批次外置、4 个真实条件的预热、实际效果、新鲜度边界、并发限制、版本摘要、验收证据与回滚方式。

## 1. 交付内容

| 文件 | 作用 | sha256 前 16 |
|---|---|---|
| `bot/side_cache.py` | 缓存读写：排他锁内“只新增未存在的键”合并、流式序列化、临时文件原子替换；文件在批次期间被改时报告 conflict，不覆盖其他写入 | `17c3cc0ee38e7f52` |
| `bot/side_inference.py` | 批量推断：缓存只解析一次、结束提交一次；`deadline` 软超时；进度写 state | `1a462fc6af479dfb` |
| `bot/side_runner.py` | 独立 oneshot 执行器：单实例锁、死进程/PID 复用接管、空闲门、内存门槛、`--max-seconds`、state | `2881fb4e7a5af005` |
| `bot/statistics_prewarm.py` | 4 条件预热：录像 100/100、200/200；卡片默认 200/200、100/100；每轮一个 key 轮转；忙碌/Side/内存跳过 | `d16c2c66d311a7a3` |
| `bot/statistics_server.py` | `side_inference.external=true` 时不启动进程内批次；`prewarm.enabled` 时启动轮转线程 | `9d2d83047ececcb5` |
| `tests/test_side_batch.py` | 19 项：并发合并、冲突不丢数据、门槛、锁接管、原子写 | — |
| `tests/test_statistics_prewarm.py` | 11 项：参数与页面逐键一致、轮转、跳过、状态观察 | — |

配置文件（生产 `config.json`，不入库）：

```json
"side_inference": {"external": true},
"prewarm": {"enabled": true, "scopes": [["100","100"],["200","200"]],
            "interval_seconds": 300, "include_recordings": true, "include_card": true}
```

## 2. 实际效果（生产实测，2026-11-06）

- Side 固定样本：旧实现 **617.9s** → 新实现 **72.5s**，新增 key 与 payload 完全一致、既有条目 0 改动。
- web 空闲 RSS：**96MB**（批次在进程内时 350–420MB）；预热期间峰值 205MB；available 最低 692MB。
- 用户同条件命中（4 个预热 key）：**22–215ms**，均带 `saved_at` 的 stale/refreshing 结果；冷条件仍 503 后按 `Retry-After` 重试。
- 卡片分桶复用：稳定选择集 32 次查找 32 命中；排名证据补齐期间 26/32、30/32 命中（失效仅限收到新 digest 的桶）。
- 预热后台成本：每圈 4 次计算合计约 **37.8s / 20 分钟 ≈ 3.1% 单核**；WAL 维持 128MB 上限、入库正常、无 OOM。

## 3. 新鲜度边界

- `fresh` 仅当 `age ≤ ttl(30s)` 且 `stamp == snapshot_version`；该版本覆盖 DB/WAL、确认表、上传会话、config、命名与引用文件，**不含排名证据**。
- 因此最坏情况是“计算后 ≤30s 内内容略旧”；30 秒后必转 stale 并排队刷新，DB 一写立即 stale，不会无限延续。
- 200 范围始终通过 `ranks_for` 现场核验，`unknown/pending` 不计入；`fresh` 不等于实时排名资格。

## 4. 并发限制（未解决项）

- 只预热 4 个组合；日期/卡片/混选等条件仍走冷路径，首请求可能 503 + 重试（实测 1.2s 返回 503）。
- 卡片宽表冷启约 16s（32 桶全空），预热只能提前到用户到来之前。
- 不同条件并发仍可能遇到 `query_busy`；同条件并发只排一次刷新（pending 去重）。
- 预热跳过（刷新忙碌、Side 锁、内存门槛）会拉长每 key 实际间隔；跳过时旧结果只按 stale 展示，不延长 TTL。

## 5. 验收证据（工作区）

- `workflow-results/statistics-query-profile-20261106/prewarm-acceptance-20261106.md`（4 轮真实计算、命中、并发、资源、计数口径、回滚）
- `workflow-results/statistics-query-profile-20261106/side-lowmem-acceptance-20261106.md`（Side 受控批次 + 定时触发）
- `workflow-results/statistics-query-profile-20261106/side-fixed-sample-result.jsonl`（旧/新同条件对照）
- `workflow-results/statistics-query-profile-20261106/prewarm-reuse-result.jsonl`（分桶复用）
- `PROJECT_HANDOFF.md` 顶部条目（当前状态）。

## 6. 回滚

Side：

```bash
systemctl disable --now side-inference.timer; systemctl stop side-inference.service
BK=$(cat /root/last-side-lowmem-backup)
cd /opt/ygobot && cp -a "$BK/bot/." bot/ && rm -f bot/side_runner.py
# config 去掉 side_inference 段（或改 external=false）
systemctl restart ygo-web
```

预热：

```bash
BK=$(cat /root/last-prewarm-backup)
cd /opt/ygobot && cp -a "$BK/statistics_server.py" bot/statistics_server.py && rm -f bot/statistics_prewarm.py data/prewarm-state.json
# 只删 config 的 prewarm 段，保留其他对话的更新
systemctl restart ygo-web
```

核对：`sha256sum` 与备份一致、`/health` 200、`ygo-web` active、config 无 `prewarm`/`side_inference.external`。
