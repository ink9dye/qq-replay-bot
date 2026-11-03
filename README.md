# 萌卡观战助手（MoeCardWatch）

把 `qq-replay-bot` 里的**远程观战端**（`tools/watch_worker.py` + `watch.py` 的协议/录制逻辑）
做成独立的 Windows 桌面程序。填好服务器、令牌和萌卡账号后，它会自动领取服务器派发的观战任务，
进入房间录制 `.yrp`，上传回服务器并上报结果。

支持 Windows 10/11 64 位；发布包不需要安装 Python。

> 这不是对局播放器，也不会渲染卡牌和战场。它的界面用于配置、观察任务进度、查看日志和管理
> 本机录像。要看录像内容，请用 YGOPro 打开保留下来的 `.yrp`。

![界面预览](assets/preview.png)

## 功能

- **图形化配置**：服务器地址、接口令牌、客户端名称、并发、萌卡账号。
- **一键观战**：检查接口 → 登录萌卡 → 长轮询领任务 → 错峰进房 → 录制 → 上传 → 上报。
- **实时任务面板**：房间、玩家、状态、已保存小局、已上传数量；双击看详情。
- **运行日志**：带颜色分级，自动脱敏令牌和密码；写入本机滚动日志。
- **安全保存**：令牌和密码用 Windows DPAPI 加密，只能由当前 Windows 用户解开；
  登录 token 只保存在进程内存，不写 `session.json`。
- **可选本机留存**：勾选后把录像复制到 `data/local-replays`，可用 YGOPro 打开。
- **并发保护**：默认并发 1；两次新连接至少间隔约 20–25 秒，避免触发平台限制。
- **单实例保护**：同一数据目录重复启动会提示，避免两个进程用同一客户端名称抢任务。
- **重复任务保护**：同一房间同时只录一份，30 分钟内不重复录。
- **停止可控**：停止会取消当前观战、断开房间、上报结果；未上传成功的临时文件会被清理。

## 直接运行源码

要求 Windows + Python 3.10+（推荐 3.12），只使用标准库，不需要安装第三方包。

```powershell
python main.py
```

首次启动会生成：

```
%LOCALAPPDATA%\MoeCardWatch\
  config.json          配置（令牌/密码为 DPAPI 密文）
  logs\client.log      滚动日志
  work\                录制中的临时文件
  local-replays\       勾选“本机保留”后的录像
```

如果 exe 同目录存在 `portable.flag`，数据会保存到 exe 同目录的 `data`，适合放 U 盘或整个文件夹拷走。

常用参数：

```powershell
python main.py --portable
python main.py --data-dir D:\MoeCardWatchData
python main.py --smoke-test
python main.py --version
```

## 打包成可分发 exe

在装有 Python 3.12 + PyInstaller 6.x 的机器上：

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

生成：

```
dist\MoeCardWatch\MoeCardWatch.exe     程序目录（含 _internal）
dist\MoeCardWatch-win64.zip            可直接发给别人
```

zip 内含 exe、`_internal`、说明和 `portable.flag`；**整个文件夹一起解压**后双击
`MoeCardWatch.exe` 即可。目标机器不需要安装 Python。不要只复制 exe，`_internal` 是运行时依赖。

> 默认提供“目录版”而不是单文件 exe：单文件需要在运行时解压 DLL，在部分安全软件、沙箱或
> 受限目录里会被拦截；目录版更稳定，也更容易排查。整个 zip 就是可分发的独立程序。

> exe 未做代码签名，Windows SmartScreen 可能提示“未知发布者”。请先核对来源和 SHA256，
> 或让接收方用源码运行。不要从陌生渠道下载二次打包的版本。

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
| 并发 | 同时录制几间；建议 1。同账号多间可能被平台拒发回放 |
| 萌卡账号/密码 | 用**自己的**萌卡账号，不要用服务器账号；只在本机登录，不上传 |

点击“测试连接”只检查 `/watch-api/status`；点击“验证账号”会真实登录萌卡，用来提前发现账号问题。

## 运行流程

1. `GET /watch-api/status` 检查服务器和令牌。
2. `watch.sign_in()` 登录萌卡，拿 `u16Secret`。
3. `POST /watch-api/claim` 长轮询任务；有任务才进房。
4. 用本局 `join_id` 在本地算入房口令，连接 `host:port`，发送 `PLAYER_INFO` + `JOIN_GAME`。
5. 收到 `STOC_REPLAY` 后保存 `.yrp`，上传到 `/watch-api/upload`；收到胜负消息时补传
   `.yrp.evidence.json`。
6. `POST /watch-api/report` 上报 `saved` / `noreplay` / `failed`。
7. 心跳每 10 秒一次；空任务时不发送空 `active` 列表（与服务端约定一致）。

## 与上游仓库的关系

本目录是从以下项目抽取的**客户端**：

- `watch.py`：协议、登录、入房口令、`Spectator`、`ReplayWriter`（原样复制）。
- `retention_policy.py`、`bot/join_throttle.py`：`watch.py` 的依赖（原样复制）。
- `reference_decks.json`：卡组参考库（原样复制）。
- 任务循环参考 `tools/watch_worker.py`，并重写为可停止、可观察的 `WatchClient`。

没有修改服务端，也不会影响原仓库的运行。服务端仍需开启 `remote.enabled` 并下发令牌。

## 测试

```powershell
python -m unittest discover -s tests -v
python main.py --smoke-test
```

测试覆盖配置、服务地址规范化、日志脱敏、DPAPI 配置读写，以及用本地假服务器跑通
“领任务 → 录制 → 上传录像/证据 → 上报”的完整链路。

## 排障

| 现象 | 处理 |
|---|---|
| 401 / 403 | 令牌不对或已轮换；向管理员要新令牌 |
| 404 / 502 / 503 | 服务器未启用远程接口或 collector 未启动 |
| 萌卡登录失败 | 账号密码错误，或本机网络无法访问萌卡 |
| 一直“无录像” | 平台没下发回放；常见于同账号多房间，先把并发调到 1 |
| 任务 409 | 服务器重启/任务过期；已上传文件通常仍会入库，继续下一个任务 |
| 上传失败 413 | 录像超过服务端 4 MB 或 nginx 限制 |
| 进房口令错误 | 确认本机账号能正常登录；不要复用服务器的 `session.json` |
| 窗口打不开/闪退 | 用 `python main.py` 从源码运行看报错；或检查杀毒软件是否拦截未签名 exe |

## 安全提醒

- 接口令牌是共享凭据，泄露后任何人都能领任务、上传文件；不要发群、不要提交仓库。
- 萌卡账号密码只在本机使用；程序不会上传，也不会写入日志。
- 配置中的敏感字段是 DPAPI 密文，但仍建议给 Windows 账户设置密码。
- 服务端地址当前是 HTTP，不要在公共 Wi-Fi 下使用不信任的服务器地址。
- 分发给别人时，只给 exe/zip 和服务器地址，不要把自己的 `config.json`、日志或账号发出去。

## 目录

```
main.py                  入口
app/
  main.py                参数、DPI、watch 路径、窗口启动
  ui.py                  Tkinter 界面
  client.py              远程观战任务循环
  config.py              配置校验/读写
  secrets.py             Windows DPAPI
  redact.py              日志脱敏
  paths.py               数据目录/滚动日志
watch.py                 上游观战协议与录制核心
bot/join_throttle.py     上游依赖
retention_policy.py      上游依赖
reference_decks.json     上游卡组参考库
assets/app.ico           应用图标
tests/                   单元测试
build.ps1                一键打包
MoeCardWatch.spec        PyInstaller 配置
```
