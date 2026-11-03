# 萌卡观战助手（MoeCardWatch）

把 `qq-replay-bot` 里的**远程观战端**（`tools/watch_worker.py` + `watch.py` 的协议/录制逻辑）
做成独立的 Windows 桌面程序。填好服务器、令牌和萌卡账号后，它会自动领取服务器派发的观战任务，
进入房间录制 `.yrp`，上传回服务器并上报结果。

**支持多账号**：填几个萌卡账号，就同时观战几间房——一个账号只负责一间，几路互不干扰。

支持 Windows 10/11 64 位；发布包不需要安装 Python。

> 这不是对局播放器，也不会渲染卡牌和战场。它的界面用于配置、观察任务进度、查看日志和管理
> 本机录像。要看录像内容，请用 YGOPro 打开保留下来的 `.yrp`。

![界面预览](assets/preview.png)

## 功能

- **多账号并行**：账号列表里加几个账号就是几路并发，每间房绑定一个账号；任务表能看到
  每条任务用的是哪个账号。
- **图形化配置**：服务器地址、接口令牌、客户端名称、账号列表可增删；密码支持一键显示/隐藏。
- **一键观战**：检查接口 → 逐个登录账号 → 长轮询领任务 → 错峰进房 → 录制 → 上传 → 上报。
- **实时任务面板**：房间、玩家、账号、状态、已保存小局、已上传数量；双击看详情。
- **运行日志**：带颜色分级，自动脱敏所有令牌和密码；写入本机滚动日志。
- **安全保存**：令牌和密码用 Windows DPAPI 加密，只能由当前 Windows 用户解开；
  登录 token 只保存在进程内存，不写 `session.json`。
- **数据在 exe 旁边**：配置、日志、录像全部落在 exe 同级的 `data` 目录，随时翻看和备份。
- **本机留存录像**：默认把录像复制到 `data/local-replays`，按日期分文件夹，YGOPro 可直接打开。
- **错峰保护**：两次新连接至少间隔约 20–25 秒，避免触发平台限制。
- **单实例保护**：同一数据目录重复启动会提示，避免两个进程用同一客户端名称抢任务。
- **重复任务保护**：同一房间同时只录一份，30 分钟内不重复录。
- **停止可控**：停止会取消当前观战、断开房间、上报结果；未上传成功的临时文件会被清理。

## 直接运行源码

要求 Windows + Python 3.10+（推荐 3.12，需带 tkinter），只使用标准库，不需要安装第三方包。

```powershell
python main.py
```

首次启动会在**项目根目录**生成 `data`：

```
data\
  config.json                    配置（令牌/密码为 DPAPI 密文）
  logs\client.log                滚动日志（2 MB 轮转，保留 3 份）
  work\                          录制中的临时文件，任务结束即清理
  local-replays\2026-11-03\      录像，按日期分文件夹
  decks\                         临时卡组文件
```

打包成 exe 后，这个目录就在 `MoeCardWatch.exe` 同级，路径不变。界面底部可以直接点开
「数据目录」和「录像目录」。

常用参数：

```powershell
python main.py --data-dir D:\MoeCardWatchData
python main.py --smoke-test      # 打开窗口 1.2 秒后自动退出，自检用
python main.py --version
```

如果 exe 所在目录不可写（比如装在 `Program Files`），程序会自动回退到
`%LOCALAPPDATA%\MoeCardWatch`，并在日志里提示实际使用的目录。

## 打包成可分发版本

在装有 Python 3.12 + PyInstaller 6.x 的机器上：

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

生成唯一产物：`release\v1.0\`，里面是

```
release\v1.0\
  MoeCardWatch.exe      双击运行
  _internal\            运行时依赖，必须和 exe 放在一起
  assets\preview.png    界面预览图
  README.md
  NOTICE.md
```

**整个文件夹一起拷走**即可，目标机器不需要 Python。不要只复制 exe，`_internal` 是运行时依赖。
发给别人时把整个 `release\v1.0` 压成 zip 即可（不要带上自己跑出来的 `data` 目录）。

> 默认提供“目录版”而不是单文件 exe：单文件需要在运行时解压 DLL，在部分安全软件、沙箱或
> 受限目录里会被拦截；目录版更稳定，也更容易排查。

> exe 未做代码签名，Windows SmartScreen 可能提示“未知发布者”。请先核对来源，或让对方用
> 源码运行。不要从陌生渠道下载二次打包的版本。

如果 PyInstaller 不在当前 Python 环境，可以指定：

```powershell
.\build.ps1 -Python "C:\Path\python.exe" -PyInstallerSite "C:\Path\site-packages"
```

## 配置说明

| 字段 | 说明 |
|---|---|
| 服务器地址 | 服务器管理员提供的地址，例如 `http://101.201.108.188:8787`；也接受裸主机名 |
| 接口令牌 | 服务端 `watch.remote.token`，不是 QQ/NapCat/统计的 token |
| 客户端名称 | 同一令牌下必须唯一，例如 `pc-你的名字`；重名会互相覆盖任务状态 |
| 萌卡账号 | 一行一个账号，最多 6 个；**用一个账号就一路，用几个账号就几路并发** |
| 萌卡密码 | 用**自己的**萌卡账号，不要用服务器账号；只在本机登录，不上传 |
| 同时观战 | 只读，等于已填账号数；一个账号同时只进一间房 |

点击“测试连接”只检查 `/watch-api/status`；点击“验证账号”会逐个真实登录萌卡账号，
提前发现哪个账号密码不对。

> 同一个账号同时进多间房时，平台可能拒发回放——所以并发要靠加账号，不要靠加大并发数。

## 运行流程

1. `GET /watch-api/status` 检查服务器和令牌。
2. 逐个 `watch.sign_in()` 登录所有萌卡账号（账号之间留 0.4 秒间隔），各拿一份 `u16Secret`；
   失败一个不影响其他账号。
3. `POST /watch-api/claim` 长轮询任务，`capacity` 等于可用账号数。
4. 给任务分配一个空闲账号，用本局 `join_id` 在本地算入房口令，连接 `host:port`，
   发送 `PLAYER_INFO` + `JOIN_GAME`。
5. 收到 `STOC_REPLAY` 后保存 `.yrp`，上传到 `/watch-api/upload`；收到胜负消息时补传
   `.yrp.evidence.json`。
6. `POST /watch-api/report` 上报 `saved` / `noreplay` / `failed`。
7. 心跳每 10 秒一次，带上所有进行中的任务；空任务时不发送空 `active` 列表。

## 与上游仓库的关系

本目录是从以下项目抽取的**客户端**：

- `watch.py`：协议、登录、入房口令、`Spectator`、`ReplayWriter`（原样复制）。
- `retention_policy.py`、`bot/join_throttle.py`：`watch.py` 的依赖（原样复制）。
- `reference_decks.json`：卡组参考库（原样复制）。
- 任务循环参考 `tools/watch_worker.py`，并重写为可停止、可观察、支持多账号的 `WatchClient`。

没有修改服务端，也不会影响原仓库的运行。服务端仍需开启 `remote.enabled` 并下发令牌。

## 测试

```powershell
python -m unittest discover -s tests -v
python main.py --smoke-test
```

测试覆盖配置与旧配置迁移、账号校验、服务地址规范化、日志脱敏、DPAPI 配置读写，以及用本地
假服务器跑通“领任务 → 录制 → 上传录像/证据 → 上报”的完整链路，还有一个双账号同时录两间房
的并发用例。

## 排障

| 现象 | 处理 |
|---|---|
| 401 / 403 | 令牌不对或已轮换；向管理员要新令牌 |
| 404 / 502 / 503 | 服务器未启用远程接口或 collector 未启动 |
| 萌卡登录失败 | 账号密码错误，或本机网络无法访问萌卡；点“验证账号”看是哪个账号 |
| 部分账号登录失败 | 其余账号照常工作；日志会写明哪个账号失败，改好密码后重启即可 |
| 一直“无录像” | 平台没下发回放；别让同一个账号进多间房，改用多账号 |
| 任务 409 | 服务器重启/任务过期；已上传文件通常仍会入库，继续下一个任务 |
| 上传失败 413 | 录像超过服务端 4 MB 或 nginx 限制 |
| 进房口令错误 | 确认该账号能正常登录；不要复用服务器的 `session.json` |
| 窗口打不开/闪退 | 用 `python main.py` 从源码运行看报错；或检查杀毒软件是否拦截未签名 exe |
| 数据没写进 exe 旁边 | exe 所在目录不可写（Program Files），已回退到 `%LOCALAPPDATA%\MoeCardWatch` |

## 安全提醒

- 接口令牌是共享凭据，泄露后任何人都能领任务、上传文件；不要发群、不要提交仓库。
- 萌卡账号密码只在本机使用；程序不会上传，也不会写入日志。
- 配置里的密码是 DPAPI 密文，**但 `data` 目录里的录像和日志是明文的**——把 `data` 发给别人
  等于把自己的观战记录和账号名一起发出去，分发前先清掉。
- 密码只有当前 Windows 账户能解；换电脑或换系统用户后需要重新填写密码。
- 服务端地址当前是 HTTP，不要在公共 Wi-Fi 下使用不信任的服务器地址。
- 分发给别人时，只给 `release\v1.0` 目录和服务器地址，不要带 `data`、日志或账号。

## 目录

```
main.py                  入口
app/
  main.py                参数、DPI、watch 路径、窗口启动
  ui.py                  Tkinter 界面（账号列表、任务表、日志）
  client.py              远程观战任务循环 + 多账号调度
  config.py              多账号配置校验/读写/旧配置迁移
  secrets.py             Windows DPAPI
  redact.py              日志脱敏
  paths.py               数据目录/滚动日志
watch.py                 上游观战协议与录制核心
bot/join_throttle.py     上游依赖
retention_policy.py      上游依赖
reference_decks.json     上游卡组参考库
assets/app.ico           应用图标
tests/                   单元测试
build.ps1                一键打包到 release\v1.0
MoeCardWatch.spec        PyInstaller 配置
release\v1.0\            打包产物（构建后生成）
```
