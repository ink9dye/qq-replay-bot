package com.moecard.watch.ui.watch

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import com.moecard.watch.ui.MainViewModel
import com.moecard.watch.ui.LogsTab
import com.moecard.watch.ui.SettingsTab
import com.moecard.watch.ui.TasksTab

/**
 * 观战机器人入口：原「萌卡观战助手」三页签（设置/任务/日志）原样保留，
 * 只是从应用主界面降级为查询端抽屉里的一个功能页。逻辑（服务、自启、日志）不动。
 */
@Composable
fun WatchScreen(vm: MainViewModel) {
    var tab by rememberSaveable { mutableStateOf(0) }
    Column(modifier = Modifier.fillMaxSize()) {
        TabRow(selectedTabIndex = tab) {
            Tab(selected = tab == 0, onClick = { tab = 0 }, text = { Text("设置 / 账号") })
            Tab(selected = tab == 1, onClick = { tab = 1 }, text = { Text("任务") })
            Tab(selected = tab == 2, onClick = { tab = 2 }, text = { Text("日志") })
        }
        when (tab) {
            0 -> SettingsTab(vm)
            1 -> TasksTab(vm)
            2 -> LogsTab(vm)
        }
    }
}
