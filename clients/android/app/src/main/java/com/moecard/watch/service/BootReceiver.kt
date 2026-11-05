package com.moecard.watch.service

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.core.content.ContextCompat
import com.moecard.watch.data.ConfigStore

/**
 * 监听开机完成 / 应用更新：若观战此前处于运行态（watch_running），则自动拉起前台服务，
 * 与 WatchService 内的看门狗、START_REDELIVER_INTENT 一起构成“被杀/重启后自动恢复”的保活链。
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        val act = intent?.action ?: return
        if (act == Intent.ACTION_BOOT_COMPLETED ||
            act == Intent.ACTION_MY_PACKAGE_REPLACED ||
            act == "android.intent.action.QUICKBOOT_POWERON"
        ) {
            if (ConfigStore(context).isWatchRunning()) {
                val i = Intent(context, WatchService::class.java).apply {
                    action = WatchService.ACTION_START
                }
                try {
                    ContextCompat.startForegroundService(context, i)
                } catch (_: Exception) {
                    // 部分 ROM 在 boot 阶段限制启动前台服务，看门狗会在之后补拉起
                }
            }
        }
    }
}
