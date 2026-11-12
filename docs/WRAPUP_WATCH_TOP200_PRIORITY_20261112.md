# 前200内战最高观战优先级收尾（2026-11-12）

## 背景与最终行为

维护者要求前200内战优先级最高，并明确授权立即部署、上传与重启。旧规则先按时间切批，早开的普通对局可能排在前200内战之前。

新规则先分两档：双方有效名次均为1–200的双人对局为第一档，其余候选为第二档。第一档整体优先，跨越原有5分钟批次边界。例如199名对200名，即使比1名对201名晚开7分钟，仍先排队。

各档独立沿用原规则：按最早开局为锚点切300秒批次（包含恰好300秒），早批优先；批内双方名次和升序，同和按开局时间、房间key排序。未知/未上榜不算前200，排名和仍按每人100000。复用既有官方榜单、有效短期观测及排名缓存，不新增网络查询。

关注名单和自动前200发现仍进入同一队列；不抢占已经在录的任务。延迟入场、空闲槽位、冷却、去重和超龄限制沿用原实现，因此“排队第一”不等于立即入房。

## 改动与验证

- `watch.py`：抽出只读缓存名次函数，新增双前200判定，候选先分档再按原规则排序。
- `tests/test_watch_priority.py`：10项测试覆盖跨批内战优先、第200名边界、未知/无效名次、各档内批次、同分、缓存来源与过期、已派发/冷却过滤、队列容量截断。
- `python -m py_compile watch.py tests/test_watch_priority.py`：通过。
- unittest按 `test_watch_priority.py`、`test_watch_rank_window.py`、`test_watch_preexisting.py`、`test_watch_unification.py`、`test_watch_remote.py` 加载：共33项通过。
- `git diff --check`：通过。

## 部署与验收

2026-11-12 11:25:57（北京时间），仅替换 `/opt/ygobot/watch.py` 并重启 `ygo-collector`，PID 1260687 → 1905841。QQ、web、NapCat与客户端未重启。维护者明确授权“现在直接”部署，操作时远程客户端仍有在录任务；本次未等待零在录窗口，未停止客户端。

写入前已重新核对线上最新版本，与本地改前基线在LF归一后完全一致；先备份，上传候选后再次逐字节比较线上当前文件与备份才覆盖，服务器执行py_compile、chown ygo:ygo后重启。

| 文件版本 | SHA256 |
|---|---|
| 线上旧文件（原始） | `babc1208ecc0df78356b9e4f3b33098a76988f5cdbe5c55d25281f32f1bb4bce` |
| 旧文件LF归一 | `c959991d2bffdf03d57164b25cdfb89dd2f5b9224b3c6a35d413b2c6029c5b8c` |
| 新文件 | `6bc9bdfd6013585d4e23bf29e43df94c2086a8ebb9c1ca5b3f394abf43e16035` |

备份：`/opt/ygobot/data/config-backups/watch-top200-20261112/watch.py.bak`。

验收：

- 线上实际源码调用候选排序，验证“199/200的新对局优先于1/201的较早对局”通过。
- 新PID已写入新鲜的 `capture-status.json`，自动排名范围200、top100启用。
- 8787/8788 `/health` 均200，collector/web/qq均active。
- 8个在线客户端心跳恢复；随后任务板已实际派发1个新任务并认领，`joined_observations=1`，尚无新增录像回传时完成初次验收。
- 后续实际复核：已派发5个、认领5个、收到3份录像上传，失败0；8端在线，启动后仍无Traceback。新文件LF归一SHA256为 `3912f874deff1107cbb652a39fc88beec67a126ba4f6b5c3335ce5c475648c4e`，本地与线上一致。
- 代码、10项新增测试、索引与收尾已推送GitHub master，功能提交 `800b1fb`。
- 本次启动后的journal无Traceback。真实任务的排序竞争没有现场逐项比对；规则正确性由测试和线上源码调用验证，派发链路另由实际认领验证。

systemd提示collector单元文件/override磁盘版本有其他未reload变更；本次未执行daemon-reload，沿用已加载单元，仅重启进程，避免加载其他工作的配置。

## 回滚与影响面

恢复上述备份至 `/opt/ygobot/watch.py`，`chown ygo:ygo`，经维护者授权后重启 `ygo-collector`；再检查健康、capture-status新PID与任务板心跳。执行回滚前先保存并比对线上最新版本，不能覆盖后来其他改动。

影响仅为新候选观战排队；统计口径、网站与QQ功能不变。源文件属于共用后端，但本次仅加载collector角色。仓库其他未提交工作未纳入本次提交。
