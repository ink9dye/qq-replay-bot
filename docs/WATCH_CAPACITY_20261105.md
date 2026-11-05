# 观战客户端带宽容量测试程序

程序：`tools/watch_capacity.py`（另有可独立下载运行的导出包）。Python 3.9+，只用标准库。本轮只做本机回环验证，没有在生产服务器安装、启动接收端或发起压测。

## 它回答什么

1. `estimate`：给定每个IP的观战路数、录像和旁证大小、回传频率、心跳频率、入站/出站带宽，计算带宽模型上限，并同时考虑集中回传。
2. `demo`：自动在本机临时端口启动测试接收端，逐级增加模拟客户端负载，输出吞吐、错误率、各接口P95和保护触发原因；结束即关闭接收端。
3. `serve` + `run`：在经许可的测试接收端与测试电脑之间测实际网络路径。接收端只统计、丢弃合成上传，不连接萌卡、不写录像或事实库，也不调用真实`/watch-api`。
4. `merge`：合并来自不同网络、同一接收端、相同负载模型、时间重叠的报告；去重接收端实际看到的来源。两台电脑若共用NAT出口，只计一个来源。

**不能把单机模拟的8组客户端说成8个真实公网IP，也不能把带宽模型当作最终能放行的IP数。** 最终还受真实collector鉴权/归属账本/入库、内存、CPU、连接数、每IP观战上限和萌卡策略影响。本工具的专用接收端不验证这些业务容量。它也不会修改当前每IP并发配置。

## 先直接运行

导出包里的`run_local_demo.cmd`可双击，约20秒，仅本机演示。也可在程序所在目录执行：

```powershell
python watch_capacity.py demo --stages 1,2,4 --stage-seconds 5 --rooms-per-ip 1 --heartbeat-seconds 1 --upload-seconds 2 --replay-kib 4 --evidence-kib 8 --max-rate-mbps 1 --max-total-mib 8 --output local-demo.json
```

这是为快速检查程序而加速的合成负载，不代表线上真实流量，更不是公网带宽实测。正常压测须按实际负载参数运行，并覆盖足够长的上传周期。

## 不联网的估算

```powershell
python watch_capacity.py estimate --ingress-mbps 10 --egress-mbps 3 --rooms-per-ip 6 --replay-kib 16 --evidence-kib 64 --upload-seconds 180 --heartbeat-seconds 20 --match-seconds 900 --reserve 0.4 --burst-seconds 10 --output estimate.json
```

这些都是输入假设，不是自动读取的生产测量值。默认每个建模IP是一名worker带6间房；若实际是多个独立worker，须考虑更多心跳与连接开销。`--baseline-in-mbps`和`--baseline-out-mbps`可额外扣除已有业务流量。默认保留40%余量，网络开销模型另加20%。录像与旁证大小必须分别估计，旁证可能比录像大。

上述示例的模型结果：平均入站224个IP、平均出站3206个IP、所有IP同时回传且须10秒内完成时12个IP。**12只是该组假设下的集中回传模型，不是已实测允许12个IP，也不是最终放行建议。** 改变旁证大小、回传周期和突发完成时间，结果会明显变化。

Mbps按百万bit/s、KiB按1024字节换算。阿里云固定公网IP的购买带宽对应出站，录像从客户端上传服务器主要占入站；官方文档说明，出站小于10 Mbit/s时入站上限为10 Mbit/s。若使用EIP、NAT或其他网络产品，不能直接套用这一条，应按实际产品配置和测量输入。来源：[阿里云ECS实例网络带宽](https://help.aliyun.com/zh/ecs/user-guide/network-bandwidth/)，核对日期2026-11-05。

## 跨机器测试（本轮未运行）

需要先安排专用接收端及安全窗口；这份说明不代表已获准给生产开端口或压测。不要指向8787真实观战入口，更不能拿真实设备令牌来测。

接收端与测试电脑设置相同、临时且独立的`WATCH_CAPACITY_TOKEN`环境变量，必须是`bench_`开头、至少22字符的ASCII随机串。程序拒绝`wt_`设备令牌形状；不读取生产配置，也不会输出测试密钥。密钥通过私密渠道设置，不写进命令行参数或报告。

经许可，在测试接收端运行：

```text
python3 watch_capacity.py serve --bind 127.0.0.1 --port 8896 --duration 300 --max-total-mib 128 --max-qps 25 --max-connections 32 --output receiver.json
```

默认只监听回环。如需直接跨网络，必须由操作者明确选择绑定地址、限制测试来源并配置相应网络入口；程序不改防火墙、nginx或安全组。也可以通过已获准的SSH隧道测试单一路径，但此时接收端看到回环来源，不能统计真实公网IP个数，结果还包含隧道开销。HTTPS客户端正常校验证书，不提供忽略证书选项。

在测试电脑执行（`TEST-HOST`替换为专用接收端）：

```text
python watch_capacity.py run --url http://TEST-HOST:8896 --allow-network --stages 1,2,4,8 --stage-seconds 30 --max-rate-mbps 1.5 --max-total-mib 64 --output agent-a.json
```

客户端必须先拿到专用协议握手才会发送负载。URL只能是站点根地址，不接受`/watch-api`、查询参数、用户密码或分享链接；非回环目标须显式`--allow-network`。不跟随重定向，不发送`X-Real-IP`伪造来源。每阶段最低要求完整覆盖认领、心跳、上传、完成，否则不会判为通过。

短阶段会在开始阶段确保每间房至少尝试一次录像与旁证回传，所以可能比真实平均流量更大。`short_stage_not_steady_state`表示没有覆盖两个上传周期；不能据此声称通过稳态容量验证。默认分散启动；`--synchronized`用于一起启动/回传的突发测试，可能先碰到接收端QPS限制。

多真实来源验证：在两台已有不同出口的测试电脑上，使用**相同参数、单一阶段**同时运行，例如都指定`--stages 4 --stage-seconds 60`。取得各自报告后：

```text
python watch_capacity.py merge agent-a.json agent-b.json --output merged.json
```

不能把同机顺序执行的1/2/4/8阶段当作同时在线来源合并。代理、隧道、内网接收端只能确认接收端可见来源，不能穿透还原公网出口。`observed_distinct_public_ips`只计算接收端确认是公网地址的来源，回环演示为0。

## 如何读报告

- `modeled_ip_groups`/`highest_passed_modeled_groups`是本次提供的模拟负载规模，不是不同公网IP数，也不是服务器最大值。
- `stage_passed`要求错误率、各接口P95和最低操作覆盖均达标。默认错误率≤1%、P95≤1500ms。
- `client_rate_limiter_engaged`为true时，客户端主动限速参与了结果；此结果只证明受限负载，不能拿来认证服务器吞吐上限。
- `limiting_signals`区分客户端限速、接收端配置QPS、资源/连接上限、延迟和操作覆盖不足。429是测试接收端配置限制，不等于带宽耗尽；503须结合接收端报告看原因。
- HTTP流量是请求/响应内容与头部的估算，不包括精确TCP重传、ACK及TLS流量。估算模式的20%开销是假设，不冒充抓包数据。
- 接收端记录实际来源的会话内散列，不保存IP明文或密钥；不声称完全匿名。公开IP最大可放行数始终标记未验证。
- 如测试中途停止，已完成的阶段仍落盘；失败运行退出码为2，成功为0。

## 小机器保护与退出

接收端默认最长300秒（可配置至3600秒）、总body流量128MiB、25请求/秒、32连接、单次上传4MiB。上传按64KiB块读取后丢弃，不写录像。客户端默认总估算HTTP流量64MiB、速率上限1.5Mbps，每阶段最多128组/1800秒；速率太低时会明确结束，不无限等待。

Linux每秒读取轻量资源计数：available<400MiB、磁盘余量<2GiB立即停止接受负载；系统CPU连续3次>85%也停止。保护触发后约2秒关闭接收端，已接受的连接另有5秒socket超时；Ctrl+C可立即结束主服务。Windows等缺Linux计数的环境，相关指标为null，不伪称CPU/内存保护已验证；时长、字节、连接数限制仍生效。

没有定时任务、开机启动、守护部署或自动重启。只产生指定JSON报告；删除导出目录或报告即可，不影响项目录像和数据库。

## 当前验证边界

本机回环HTTP与离线模型已验证，见交接所指向的本轮测试证据。未测云端带宽、真实不同公网出口、真实watch API及入库开销，也未解除原有浏览器权限拒绝。既有令牌/缓存候选仍未部署；浏览器点击验收和生产加载继续单独登记。
