# 饼图交集展示修复收尾（2026-11-12）

用户报告无垢大艺术、狱神各占9.5%，却没有交集阶梯。原缓存中两者共同17人；统计均摊正确，展示漏画。

## 改动

- 在均摊后最终排序上计算交集；将构筑全部字段投影到可见切片，汇总每对字段，每份构筑对同一字段对只计一次。
- 网页和图片共用后端bands；取消仅支持一个extras的补画逻辑。相邻交集画阶梯，不相邻交集保留图例。
- players保留真实人数，display_players限制阶梯宽度不超过任一侧切片份额，避免越界。图片缓存版本21→22。
- 不改命名算法、字段门槛、切片资格或人数均摊；不改安卓、QQ链路或NapCat。

## 验证

- `python -m py_compile bot/deck_pie.py bot/top100_pie.py bot/statistics_pie_image.py` 通过；提取网页脚本的`node --check`通过。
- `python -m unittest tests.test_pie_intersections tests.test_pie_card_art`：4项通过。旧12项测试中1失败2错误，已用HEAD源码复现，非本次引入。
- 原线上缓存重算共同17人、两片各9.5%，全部切片份额与旧实现一致。
- 现有rsvg-convert真实生成候选PNG；桌面及390px手机视口视觉验收通过。网页与图片7条主切片路径逐字符串完全相同。
- 上线后重建现有roster预计算，21.8秒成功。运行中`/statistics/api/roster`返回200、precomputed，最新数据共同18人，交集包含display_players=10；人数变化来自最新数据，与原17人缓存不同。
- 运行中`/statistics/api/deck-pie.png`返回200、810913字节PNG，已刷新网站共用的饼图/图例文件。8787和8788健康均200，web/qq/collector均active。

## 部署与版本

维护者明确授权部署、上传与收尾。部署前四文件生产LF摘要均与HEAD基线一致，并与本轮实际备份逐字节核对。备份：`/opt/ygobot/data/config-backups/pie-intersection-20261112/`。上传、chown ygo:ygo、语法检查后，在capture-status的active_observations=0时仅重启web：1989213→1991394；QQ 1933933、collector 1989212不变。

systemd提示磁盘上已有服务配置变动，本次未daemon-reload，避免加载其他会话配置。

| 文件 | 原LF SHA256前16 | 上线LF SHA256前16 |
|---|---|---|
| bot/deck_pie.py | 14546d2feb0c6e27 | d6b13a226a211cf6 |
| bot/top100_pie.py | dfaed08ee8f62f46 | 3898a2f353956fce |
| bot/statistics_portal.html | 87583c383761e77a | aa23ac3b3aee2898 |
| bot/statistics_pie_image.py | ce567966897d084d | e99bfb2ef3753a51 |

## 回滚与影响

只影响统计网页、共用饼图PNG及QQ下次取用的图片。浏览器可能需Ctrl+F5；QQ无需重启。回滚前先diff线上最新版本，再恢复上述备份中实际四文件、chown并经维护者许可重启web；现有预计算需按旧代码重新生成。不回滚数据库，不整目录覆盖。

证据：本地`workflow-results/pie-intersection-20261112/`，含REPORT、实际PNG、桌面/手机截图及候选补丁；临时产物不入库。
