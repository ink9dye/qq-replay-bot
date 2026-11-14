# 单卡胜率：前百饼图人数Top5收尾（2026-11-14）

用户授权部署、上传与写收尾文档。QQ `单卡胜率 卡名` 保留总体、先后攻和原胜率Top5，新增前百饼图人数最多5个具体卡组的使用后胜率，显示占比、人数和胜负平样本数。使用dashboard.population的最新有效首局人口，不合并大类或“其他”；未识别计占比分母但不占具体卡组名额。人数同数按名称排序；具体对阵按历史小局对手卡组、双方当前前100、主卡实际使用、平局计分母，不足10局标样本少，0局显示暂无有效样本。

- 本地：Python3.14编译、17项相关测试、git diff --check通过；旧card_statistics扩大测试在原线上文件与候选下均10失败1错误，未宣称全量通过。
- 服务器：Python3.12编译和5项专项测试通过。部署前重新校验原始SHA、备份并逐字节比对，确认active_observations=0；仅替换bot/card_winrate.py并chown ygo:ygo，仅重启ygo-qq，PID2277147→2414471，三个业务服务active、8787 health=200。未重启NapCat/web/collector，未改安卓、数据库或其他会话候选，未主动发QQ消息。
- 原始SHA前16：旧3ece18f8af9f69a1，新5907baf4e972a8f6；部署备份 `/opt/ygobot/data/config-backups/card-winrate-popular-20261114-223650/card_winrate.py`。提交前候选与线上字节一致；原基线LF归一与改前仓库一致。完整raw/LF摘要见证据manifest.json。
- 影响：QQ文本新增一段；共用collect的查询API附带popular_matchups字段，旧matchups/order保持。每次结果缓存未命中额外读取一次population，生产耗时不作提速承诺；真实QQ指令回复仍待用户使用验收。
- 回滚：先diff最新线上与本轮备份、保全后续修改，再恢复部署时实际备份、chown/编译，经授权仅重启ygo-qq并检查健康/在线；不回滚数据库，不重启NapCat。
- 证据：`D:/pythons/qq-replay-bot/workflow-results/card-winrate-popular-20261114/`，含候选、原线上文件、manifest、deployment、回归基线比较和live-verification。
- 额度：起1%，末次6%，累计约5个百分点，已停止新增实现与扩大检查，仅完成上传/交接。

实际库验证边界：一次受控灰流丽计算加旧版对照设置55秒进程上限，预算内未完成、进程已退出；未获得完整真实库结果，不能声称真实库新旧一致或QQ端到端验收通过。服务器部署前load约3.41/2.27/2.53，新增人口读取成本仍待低负载时复核。本轮按额度约束不再重复重查询。
