# 安卓查询 API 补齐收尾（2026-11-12）

## 结果

2026-11-12 19:33:39（北京时间）已部署并重启 ygo-web、ygo-collector。GitHub master 功能提交 `076ada2`，已有观测依赖归档 `7536876`。本次只补服务端，不改安卓代码、不构建APK。QQ及NapCat未重启，QQ PID仍1933933。

7个端点已可用：GET/POST binding、POST binding/unbind、GET player/{name}/side-plan、GET card/{code}/usage、flow、winrate。胜率API默认Top20，limit=1..20；QQ默认Top5保持原行为。绑定共享qq_bindings，只设默认查询玩家，不验证MC账号所有权。

## 部署与版本

部署前生产最新文件与本轮基线逐字节一致，先备份并复核，再原子替换6个文件，设置ygo:ygo/644，编译通过。保留线上已部署的portal观测锁及web_perf_metrics依赖。未改配置、nginx或systemd定义；systemctl提示磁盘单元有其他任务变动，本轮未daemon-reload。

用户明确授权本次在远程在录状态下重启collector。部署前本地active_observations=0、远程13路在录；未停止客户端。web PID1941319→1979333，collector PID1905841→1979336。

备份：`/opt/ygobot/data/config-backups/query-api-android-20261112/`。

| 文件 | 部署原始SHA256前16 | LF归一SHA256前16（Git对比） |
|---|---|---|
| bot/query_api.py | 6e3904af6be5a660 | 9ac178f90b1cda5d |
| bot/query_core.py | c3c1631502c131b6 | 175cf1323c778b9a |
| bot/statistics_portal.py | ce9fb755a8f10312 | 1a598f7c5119cf73 |
| bot/card_usage_pie.py | a6f120fa37aaa695 | d5a45889df8f78c1 |
| bot/card_flow.py | 0aa7a6999c131b1b | ad5613a060ebfdc6 |
| bot/card_winrate.py | 3ece18f8af9f69a1 | b3f6e1e8e3930022 |

## 验证

- 本地新增12项测试及相关语法检查通过；服务器Python3.12.3隔离48项通过。旧查询76项中7项失败在改前线上基线同样复现，本次未新增失败；未为修旧用例扩大修改。
- 公网8443使用服务器证书进行TLS校验，无跳过验证：测试账号登录200，绑定/改绑/读取/解绑/重复解绑正确；直接读共享表确认一致。临时测试账号、令牌和绑定已清理，未保存任何明文凭据，未发送真实QQ消息。
- usage、flow、winrate、side-plan真实请求均200。灰流丽胜率默认返回20条、limit=5返回5条，总体/先后攻统计完全一致；缓存命中generated_at不刷新。
- 无令牌的新GET接口401；未知卡号404；limit=21为400；公网管理路径404。Windows外部访问8443/binding亦401，证书校验成功。
- 8787/8788健康200，web/collector/qq均active，watch-status新鲜（复核时2.7秒）；新collector派发2、认领2、上报2、上传2、失败0，9端心跳、上报在录14。两项重启服务启动后无Traceback。
- 复核6个线上文件摘要一致，提交前Git内容LF归一与部署摘要一致。

## 性能与边界

冷查usage和winrate各出现一次8秒左右的503，有限重试后成功；没有扩大超时或改冻结统计策略。实测usage成功约0.03秒/5008B、flow约3.12秒/31962B、winrate Top20成功约5.25秒/4681B、Top5约3.66秒/1810B、缓存约0.025秒；side-plan约1.13秒/5195B。上述为本次样本，不保证任意卡牌恒定耗时。客户端应支持冷查503及429的有界退避重试，避免并发反复刷新。

尚未验证安卓真实UI操作，也未发送QQ命令逐屏比对；已验证服务端共享绑定及Top5/Top20的相同统计分母与座位分组。不把这次上线等同于既有机器人卡顿问题已修复。

## 回滚

恢复前先diff线上最新文件与本次部署摘要，防止覆盖后续工作；如存在变化先合并。只恢复上述6个文件的本轮生产备份、chown ygo:ygo、经授权重启web/collector并健康验收。保留数据库、绑定记录、录像、认证记录及观战账本，不恢复数据库，不重启QQ/NapCat。

## 证据与后续

完整摘要、脱敏公网请求状态/耗时、重启后状态及基线回归对照，位于工作区 `workflow-results/query-api-android-20261112/`；其中原candidate/manifest为开发阶段证据，当前状态以deployment-manifest.json及本收尾为准。接口契约见 [QUERY_API_ANDROID_20261112.md](QUERY_API_ANDROID_20261112.md)。本轮已完成用户授权的服务端补齐和上线，不追加优化。
