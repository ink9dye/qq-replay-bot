package com.moecard.watch.ui.query

import android.Manifest
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import com.moecard.watch.data.DownloadSaver

/**
 * 「保存到下载」前的权限闸门：只在 Android 8–9 且没有存储权限时弹系统授权框，
 * Android 10+ 直接执行。不管用户授不授权都会继续执行 [onReady]——
 * 没授权时 [DownloadSaver] 会退回应用目录并如实提示位置。
 */
@Composable
fun rememberLegacyStorageGate(): (onReady: () -> Unit) -> Unit {
    val ctx = LocalContext.current
    var pending by remember { mutableStateOf<(() -> Unit)?>(null) }
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { _ ->
        val action = pending
        pending = null
        action?.invoke()
    }
    return { onReady ->
        if (DownloadSaver.needsLegacyPermission(ctx)) {
            pending = onReady
            launcher.launch(Manifest.permission.WRITE_EXTERNAL_STORAGE)
        } else {
            onReady()
        }
    }
}
