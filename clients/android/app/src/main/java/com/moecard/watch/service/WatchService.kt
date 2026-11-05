package com.moecard.watch.service

import android.app.AlarmManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.wifi.WifiManager
import android.os.Build
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.content.getSystemService
import com.moecard.watch.ui.MainActivity
import com.moecard.watch.R
import com.moecard.watch.data.ConfigStore
import com.moecard.watch.domain.WatchManager

/**
 * 前台服务：把观战客户端放到前台运行，切到后台或锁屏也能继续录制上传。
 *
 * 息屏/后台保活策略（三层）：
 *  1. 前台服务 + dataSync 类型，避免被系统回收；
 *  2. PARTIAL_WAKE_LOCK + WifiLock，息屏后 CPU/WiFi 不断；
 *  3. 被杀后自动拉起：START_REDELIVER_INTENT 让系统重建服务，配合 AlarmManager 看门狗
 *     （每 15 分钟探活，发现没跑且用户仍在运行态就拉起）+ 开机自启（autoStart）。
 *
 * 另外监听默认网络：只有真正换网/断网时才主动断开活跃连接（加速重连），
 * 信号强弱、带宽变化等抖动一律不干预——连接断了也会由客户端任务内自动重连续看。
 */
class WatchService : Service() {

    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var netCallback: ConnectivityManager.NetworkCallback? = null
    /** 当前默认网络，用于识别"真的换网了"。 */
    private var lastNetwork: Network? = null
    private var lastDropAt = 0L

    override fun onBind(intent: Intent?) = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // 系统重建（被 LMK 杀死后 redeliver）时 intent 可能为 null，按启动处理；
        // 看门狗也用 ACTION_PING 探活，同样按启动处理。
        val action = intent?.action
        if (action == ACTION_STOP) {
            doStop()
            return START_NOT_STICKY
        }
        // ACTION_START / ACTION_PING / null 都视为“应当运行”
        val cfg = ConfigStore(this).apply { setWatchRunning(true) }.load()
        startForeground(NOTIF_ID, buildNotification("萌卡观战助手运行中（息屏保活）"))
        acquireLocks()
        scheduleWatchdog()
        registerNetworkCallback()
        WatchManager.start(cfg)
        return START_REDELIVER_INTENT
    }

    private fun doStop() {
        ConfigStore(this).setWatchRunning(false)
        WatchManager.stop()
        unregisterNetworkCallback()
        cancelWatchdog()
        releaseLocks()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    // ---------------------------------------------------------------- 锁
    private fun acquireLocks() {
        try {
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = pm.newWakeLock(
                PowerManager.PARTIAL_WAKE_LOCK,
                "MoeCardWatch::SpectateWake",
            ).apply {
                setReferenceCounted(false)
                acquire(6 * 60 * 60 * 1000L) // 最长 6 小时自动释放，防泄漏
            }
        } catch (_: Exception) {
            wakeLock = null
        }
        try {
            val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "MoeCardWatch::SpectateWifi")
                .apply {
                    setReferenceCounted(false)
                    acquire()
                }
        } catch (_: Exception) {
            wifiLock = null
        }
    }

    private fun releaseLocks() {
        try { wakeLock?.release() } catch (_: Exception) { }
        try { wifiLock?.release() } catch (_: Exception) { }
        wakeLock = null
        wifiLock = null
    }

    // ---------------------------------------------------------------- 网络变化监听
    /**
     * 只盯「默认网络」的**真实切换/断开**，不再因为信号强度、带宽、已用流量等
     * capability 抖动而断开观战连接。
     *
     * 旧实现监听的是 onCapabilitiesChanged，这个回调在 WiFi 信号稍微波动、
     * 网速统计刷新时都会触发（部分设备几秒一次），导致刚连上的观战被反复掐断。
     * 现在连接断了也会由 WatchClient 的任务内重连兜住，所以这里只需要在真正换网时
     * 提醒一下（加速重连），其余情况都别插手。
     */
    private fun registerNetworkCallback() {
        val cm = getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        netCallback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                val prev = lastNetwork
                lastNetwork = network
                if (prev != null && prev != network) {
                    dropConnections("默认网络已切换（$prev → $network）")
                }
            }

            override fun onLost(network: Network) {
                if (lastNetwork == network) lastNetwork = null
                // 只有在彻底没有任何可用网络时才主动断开，避免切换过程中的重复断开
                val active = try {
                    cm.activeNetwork
                } catch (_: Exception) {
                    null
                }
                if (active == null) dropConnections("网络已断开")
            }

            override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) {
                // 刻意不处理：带宽/信号波动属于正常抖动，断开只会让观战反复中断
            }
        }
        try {
            cm.registerDefaultNetworkCallback(netCallback!!)
            lastNetwork = cm.activeNetwork
        } catch (_: Exception) {
            netCallback = null
        }
    }

    /** 主动断开活跃连接以加速重连（5 秒内只做一次，避免回调风暴）。 */
    private fun dropConnections(reason: String) {
        val now = SystemClock.elapsedRealtime()
        if (now - lastDropAt < DROP_COOLDOWN_MS) return
        lastDropAt = now
        Log.i(TAG, "网络变化：$reason")
        WatchManager.dropActiveConnections()
    }

    private fun unregisterNetworkCallback() {
        val cb = netCallback ?: return
        try {
            val cm = getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
            cm.unregisterNetworkCallback(cb)
        } catch (_: Exception) { }
        netCallback = null
    }

    // ---------------------------------------------------------------- 看门狗
    private fun watchdogIntent(): PendingIntent {
        val i = Intent(this, WatchService::class.java).apply { action = ACTION_PING }
        val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) PendingIntent.FLAG_IMMUTABLE else 0
        return PendingIntent.getService(this, WATCHDOG_REQ, i, flags)
    }

    private fun scheduleWatchdog() {
        val am = getSystemService(Context.ALARM_SERVICE) as AlarmManager
        try {
            am.setInexactRepeating(
                AlarmManager.ELAPSED_REALTIME_WAKEUP,
                SystemClock.elapsedRealtime() + WATCHDOG_INTERVAL,
                WATCHDOG_INTERVAL,
                watchdogIntent(),
            )
        } catch (_: Exception) { }
    }

    private fun cancelWatchdog() {
        try {
            val am = getSystemService(Context.ALARM_SERVICE) as AlarmManager
            am.cancel(watchdogIntent())
        } catch (_: Exception) { }
    }

    // ---------------------------------------------------------------- 生命周期兜底
    override fun onTaskRemoved(rootIntent: Intent?) {
        // 用户从最近任务划掉本 App 时，若仍在运行态则立即重启，抵抗厂商杀后台
        if (ConfigStore(this).isWatchRunning()) {
            try {
                val i = Intent(this, WatchService::class.java).apply { action = ACTION_START }
                startService(i)
            } catch (_: Exception) { }
        }
        super.onTaskRemoved(rootIntent)
    }

    override fun onDestroy() {
        unregisterNetworkCallback()
        releaseLocks()
        super.onDestroy()
    }

    // ---------------------------------------------------------------- 通知
    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val mgr = getSystemService(NotificationManager::class.java)
            if (mgr.getNotificationChannel(CHANNEL_ID) == null) {
                val ch = NotificationChannel(
                    CHANNEL_ID,
                    getString(R.string.channel_name),
                    NotificationManager.IMPORTANCE_LOW,
                ).apply { description = getString(R.string.channel_desc) }
                mgr.createNotificationChannel(ch)
            }
        }
    }

    private fun buildNotification(text: String): Notification {
        val contentIntent = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_launcher)
            .setContentIntent(contentIntent)
            .setOngoing(true)
            .build()
    }

    companion object {
        const val ACTION_START = "com.moecard.watch.START"
        const val ACTION_STOP = "com.moecard.watch.STOP"
        const val ACTION_PING = "com.moecard.watch.PING"
        private const val CHANNEL_ID = "watch_channel"
        private const val NOTIF_ID = 1
        private const val WATCHDOG_REQ = 1001
        private const val WATCHDOG_INTERVAL = 15 * 60 * 1000L // 15 分钟探活一次
        private const val DROP_COOLDOWN_MS = 5_000L
        private const val TAG = "MoeCardWatch"
    }
}
