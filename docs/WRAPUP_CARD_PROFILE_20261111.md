# 卡片模糊搜索与合并资料收尾（2026-11-11）

已部署：使用量/单卡流向/单卡胜率支持百鸽模糊搜索、唯一命中直接执行、多候选数字选卡和 next 翻页。卡片 关键词 将卡图效果、使用量、流向、胜率合并为一条聊天记录；失败回退逐段图文。沿用统计口径、身份归一和卡图来源。

验证：31 项定向测试通过（离线卡图 mock）、py_compile、diff --check 通过。旧查卡断网提示断言、旧统计测试缺 stat_roster_state，已用 HEAD 原代码复现，未作为本轮回归。

用户要求部署、上传及收尾。生产五文件写入前与 HEAD 逐一核对 LF SHA256 一致；备份 /opt/ygobot/data/config-backups/card-profile-20261111/；active_observations=0，available 357MB，空闲17GB。scp、chown ygo:ygo、线上 py_compile 后仅重启 ygo-qq，active、8787 health=200；NapCat/web/collector 未重启。实际 QQ 展示待正常业务查询验收，未主动发送测试消息。

LF SHA256 前16：service d97bc2f686884d7b；commands a07539b86ef21f68；card_search c4e72cac75ac63d5；help e76bca79718a1c67；card_usage_pie 3c39ab64d73bce71。

回滚：从上述备份恢复五文件至 bot/，chown ygo:ygo，确认空闲后按授权重启 ygo-qq。
