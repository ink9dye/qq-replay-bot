# MoeCardWatch Android（萌卡观战助手 · 安卓端）

PC 端 [MoeCardWatch](../ygowatch/)（`watch.py`）的 Android 移植版：自动认领调度服务器的观战任务、
以萌卡账号连入 YGOPro/Moecard 对局房间，静默观看并录制 `.yrp` 录像、实时上传回服务器。

Kotlin + Jetpack Compose + 前台服务实现，锁屏/息屏/切后台均可持续工作。

---

## 功能特性

- **多账号并发**：配置 N 个萌卡账号，即获得 N 路并发——每个账号同一时间只看一场对局（与 PC 端改进后的语义一致）。
- **全自动任务循环**：`status` → `heartbeat`(10s) → `claim` → 观战 → `upload` → `report`，与调度服务器 `/watch-api/*` 全接口对齐。
- **协议逐字节对齐 PC 端**：
  - YGOPro TCP 帧 `<len:2 LE><proto:1><body>`，`STOC_REPLAY` 取录像、`CTOS_HS_TOOBSERVER` 观战握手；
  - Mycard 房间口令生成 `encodeMycardPassword(secret, action, suffix)` 与 PC 完全一致（7 个 JVM 单元测试交叉验证）；
  - `yrp1/yrp2` 录像头识别与拆分；`X-Players` 头 URL 编码（支持中文玩家名）。
- **息屏保活（四层）**：
  1. 前台服务（`dataSync` 类型）；
  2. `PARTIAL_WAKE_LOCK` + `WifiLock`（WiFi 高性能模式）；
  3. 系统杀死后自动拉起：`START_REDELIVER_INTENT` + `AlarmManager` 看门狗（15 分钟探活）+ `onTaskRemoved` 自愈；
  4. 开机 / 应用更新后自动恢复运行。
- **网络切换应对**：监听默认网络，WiFi ↔ 移动数据切换时主动断开活跃连接，秒级重连而非等待超时；
  断线期间任务不会结束，客户端在任务内自动重连续看。
- **网络抖动不再中断观战（1.0.1）**：
  - **任务内自动重连**：TCP 掉线/读超时不再直接结束任务，而是在同一任务里指数退避
    （2s→4s→8s→16s→30s，最多 20 次）重连同一间房，重连前自动刷新萌卡密钥；
    只有「连续 3 次都连不进去」才判定房间已结束。
  - **崩溃隔离**：协程作用域改为 `SupervisorJob` + 异常处理器，单个任务的异常
    （如结果上报超时）不会连锁杀死心跳/认领循环——旧版本正是这样「静默停摆」的。
  - **只对真实换网响应**：改为 `registerDefaultNetworkCallback`，只在默认网络真的切换/
    彻底断开时断开活跃连接（5 秒去重）；信号强弱、带宽变化等 capability 抖动一律不干预。
  - **上传可补传**：录像/证据上传失败会进补传队列，独立协程每 15 秒重试，网络恢复后自动补齐。
  - **失败不拉黑**：去重表只记录「确实录到录像」的房间，抖动导致的失败房间不会被跳过 30 分钟。
  - **看门狗自愈**：客户端 5 分钟无心跳自动重启，避免「界面显示运行中、实际已停止观战」。
- **安全存储**：令牌与萌卡密码使用 `EncryptedSharedPreferences`（AES256-GCM，Android 版 DPAPI 等价物）加密。
- **本地留存**：录像与证据 JSON 保存在应用私有外部存储（`Android/data/com.moecard.watch.debug/files/`），可用文件管理器直接查看；可选「不保留本地副本」。

## 界面

三个标签页：

| 页签 | 内容 |
|---|---|
| **设置 / 账号** | 服务器地址、接口令牌、客户端名称、萌卡账号列表（增删）、保留录像/自动开始开关、「测试连接」、「开始/停止观战」 |
| **任务** | 统计卡片（在线 / 待认领 / 本端活跃 / 已保存 / 已上传 / 失败）+ 任务列表（实时状态：连接中 → 观战中 → 已完成/未完成） |
| **日志** | 按时间着色的运行日志（成功绿 / 警告黄 / 错误红）；打开即定位到最新一条，停在底部时自动跟随新日志 |

## 构建环境

- JDK 17
- Android SDK（compileSdk 34）+ Build-Tools 34.0.0
- Gradle 8.7（AGP 8.5.2 / Kotlin 1.9.24）
- 本机使用 D 盘离线工具链时：

```bash
export JAVA_HOME="D:/dev/jdk-17.0.2"
export ANDROID_HOME="D:/android-sdk"
export ANDROID_SDK_ROOT="D:/android-sdk"
export PATH="/d/gradle-8.7/bin:$JAVA_HOME/bin:$PATH"

cd MoeCardWatch-Android
gradle :app:assembleDebug          # 调试包
gradle :app:testDebugUnitTest      # 7 个协议单元测试
```

产物：`app/build/outputs/apk/debug/app-debug.apk`

> Maven 仓库已配置阿里云镜像（google/public/gradle-plugin），国内网络可离线快速构建。

## 安装

```bash
adb install -r app-debug.apk        # 真机（开启 USB 调试）或模拟器
# MuMu 模拟器：adb connect 127.0.0.1:7555 后执行同样命令
```

或直接把 APK 拷入手机点击安装（允许「安装未知应用」）。

## 使用

1. 打开 App，在「设置 / 账号」页填写：
   - **服务器地址**：调度服务器，如 `http://101.201.x.x:8787`
   - **接口令牌**：服务器配置的 Bearer Token
   - **萌卡账号**：点「+ 添加账号」录入账号与密码（可添加多个，数量 = 并发路数）
2. 点「**测试连接**」确认服务器可达（应提示「连接成功 · 在线 N · 待认领 M」）。
3. 首次启动会请求**通知权限**；点「开始观战」会引导设置**忽略电池优化**——建议允许，否则息屏后可能被 Doze 掐断网络。
4. 之后保持 App 在后台即可；任务进度在「任务」页实时可见，录像由服务器接收。

### 厂商 ROM 注意事项

小米（MIUI/澎湃）、华为（鸿蒙）、OPPO/vivo 等激进省电策略下，除上述电池优化白名单外，
建议再开启系统的「**自启动**」权限（设置 → 应用管理 → 萌卡观战助手 → 自启动），否则看门狗可能被拦截。

## 目录结构

```
MoeCardWatch-Android/
├── settings.gradle / build.gradle          # 工程配置（Aliyun 镜像，AGP 8.5.2）
├── app/build.gradle                        # 模块配置（compileSdk 34、OkHttp 4.12、security-crypto）
└── app/src/
    ├── main/AndroidManifest.xml            # 权限 + WatchService / BootReceiver 注册
    └── main/java/com/moecard/watch/
        ├── WatchApplication.kt             # 应用入口：初始化数据目录、全局网络开关
        ├── model/                          # Account / AppConfig / RoomInfo / WatchJob / LogEntry / ConnectionInfo
        ├── protocol/                       # 协议核心（纯 JVM，可单测）
        │   ├── YgoProtocol.kt              #   TCP 帧、CTOS/STOC 常量、mycard 口令
        │   ├── Spectator.kt                #   观战连接（连接成功回调、abort 快速断连）
        │   └── ReplaySplitter.kt           #   yrp1/yrp2 录像拆分
        ├── net/
        │   ├── Http.kt                     #   共享 OkHttp 客户端
        │   ├── MoecardAuth.kt              #   萌卡登录 + u16Secret（含详细错误透传）
        │   └── WatchApi.kt                 #   调度服务器 5 接口 + 测试连接
        ├── domain/
        │   ├── WatchClient.kt              #   账号池、心跳/认领循环、单局观战、录像保存上传
        │   └── WatchManager.kt             #   应用级单例状态中枢（Flow）
        ├── data/ConfigStore.kt             #   加密配置存储 + 运行态标记
        ├── service/
        │   ├── WatchService.kt             #   前台服务：锁、看门狗、网络监听
        │   └── BootReceiver.kt             #   开机/更新自启
        └── ui/
            ├── MainActivity.kt             #   Compose 主界面（三页签）
            ├── MainViewModel.kt            #   状态桥接（ConfigStore ↔ WatchManager）
            └── theme/Theme.kt
    └── test/java/com/moecard/watch/ProtocolTest.kt    # 7 个协议交叉验证测试
```

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| 「测试连接」失败 | 查看 App 内「日志」页红字：令牌错误 / 服务器地址错误 / 网络不通会分别给出具体原因 |
| 萌卡登录失败 | 日志会透传萌卡服务器原始报错（如 `i_user_unexists` 账号不存在、`i_password_error` 密码错误） |
| 任务显示「未完成」后自动恢复 | 房间安静期超时重连，属正常重试机制（对齐 PC 端 30/120/25s 三档超时） |
| 任务卡片出现「重连中（第 N 次）」 | 网络抖动导致连接断开，客户端正在同一任务内退避重连，通常几秒内回到「观战中」，无需操作 |
| 日志出现「已排队待网络恢复后补传」 | 上传时正好断网，录像已存本地并进入补传队列，网络恢复后自动补传（每 15 秒重试一次） |
| 日志出现「N 秒无心跳，自动重启恢复」 | 客户端看门狗自愈：长时间无心跳时自动重启客户端，正在进行的任务会被服务器重新派发 |
| 息屏后中断 | 确认已允许「忽略电池优化」；小米/华为等还需开启「自启动」 |
| 切换 WiFi/流量后短暂中断 | 已内置秒级重连；若任务卡片短暂显示「连接中断」后恢复「观战中」即为正常 |
| 想彻底停掉 | 点「停止观战」——运行态标记会被清除，看门狗/开机自启不会再拉起 |

## 与 PC 端的差异

| 项 | PC 端 | Android 端 |
|---|---|---|
| 配置存储 | 文件（DPAPI） | EncryptedSharedPreferences |
| 数据目录 | exe 同级目录 | 应用私有外部存储（可在文件管理器查看） |
| 并发模型 | N 账号各看 1 场 | 相同 |
| 网络栈 | Python requests | OkHttp（禁用 keep-alive，规避系统 NPE） |
| 保活 | 进程常驻 | 前台服务 + 锁 + 看门狗 + 自启 |

## 版本

- **1.0.1**：抗网络抖动。任务内自动重连（指数退避 + 最多 20 次）、协程崩溃隔离（SupervisorJob）、
  网络回调只看默认网络真实切换、上传失败补传队列、失败房间不再被去重表拉黑、客户端无心跳自愈重启。
- **1.0.0**：首个 Android 版本。多账号并发、协议对齐、息屏保活、被杀拉起、网络切换重连、
  中文玩家名上传修复、测试连接线程修复。
