# QQ 单卡胜率先后攻拆分收尾（2026-11-11）

状态：17:05:29 已部署并重启 `ygo-qq`，MainPID 1717436。用户本轮授权修改、部署上传及收尾；仅重启 QQ 业务服务，未操作 NapCat 或登录环境。

## 改动与口径

`单卡胜率 卡名` 在总体之后新增先攻、后攻使用后胜率，均显示胜/负/平和局数；不足10局标“样本少”，无样本显示暂无有效样本。先后攻取每个小局玩家视角的 `first_player`，不是整场首局；未知座位只进总体，有有效样本时单独提示其局数。

继续使用双方当前前100、主卡实际使用、胜负已知（平局进分母）的既有条件。使用含召唤尝试及发动卡片/效果，不要求成功结算，不表示因果提升。保留总体及对手卡组 Top5 的原有口径、排序与门槛；复用同一批事实与对手，未新增人口扫描、数据库表或依赖。网页本轮未改。

## 验证

- 本地 `py -3 -m py_compile bot/card_winrate.py tests/test_card_winrate.py` 通过。
- `py -3 -m unittest tests.test_card_winrate tests.test_qq_new_commands`：7项通过。覆盖逐局座位变化、平局、未知座位、未知胜负、未使用与榜外对手排除、空样本及文字展示。
- 线上真实库灰流丽：新旧 `matchups` 结果完全一致（含总体、Top5、区间、备注）；分组胜负平及局数逐项相加等于总体。
- 总体 362胜/310负/0平，672局，53.9%；先攻223胜/108负，331局，67.4%；后攻139胜/202负，341局，40.8%；未知座位有效样本0。
- 写入前备份，下载基线与仓库做LF归一化对比一致；安装前再次比较线上与备份完全一致。录制状态新鲜且 `active_observations=0` 后安装并重启。
- 线上 `py_compile` 通过；QQ/web/collector active，8787 health=200。未主动向群发测试消息，真实群指令端到端回复待用户使用验收。
- 证据：`workflow-results/card-winrate-order-20261111/production-comparison.txt`、`deployment.txt`。

## 版本、部署与回滚

- 唯一生产改动：`/opt/ygobot/bot/card_winrate.py`。
- 旧SHA256前16：`5b76161321195f90`；新SHA256前16：`65f5348715e50454`。
- 新文件按LF归一化SHA256前16：`8956562b63d42104`（线上原文件为CRLF；Git存储为LF）。GitHub代码提交 `0a4d99d` 已推送master；重启后QQ状态online。
- 备份：`/opt/ygobot/data/config-backups/card-winrate-order-20261111/card_winrate.py`。
- 回滚前先比较当前线上摘要，保全后续修改；确认可回退后，以备份恢复该文件，`chown ygo:ygo`、`py_compile`，在无录制窗口重启 `ygo-qq` 并检查health及QQ在线状态。
- 影响面：QQ单卡胜率文字回复及其只读分组统计；web/collector代码、Top5、取源与命名策略不变。
