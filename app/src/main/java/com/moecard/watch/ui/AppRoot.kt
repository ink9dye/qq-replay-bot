package com.moecard.watch.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.EmojiEvents
import androidx.compose.material.icons.filled.Menu
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material.icons.filled.Person
import androidx.compose.material.icons.filled.PieChart
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Visibility
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalDrawerSheet
import androidx.compose.material3.ModalNavigationDrawer
import androidx.compose.material3.NavigationDrawerItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.rememberDrawerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import coil.compose.AsyncImage
import com.moecard.watch.query.net.QuerySession
import com.moecard.watch.ui.query.CardDetailScreen
import com.moecard.watch.ui.query.DeckDetailScreen
import com.moecard.watch.ui.query.LoginScreen
import com.moecard.watch.ui.query.PieScreen
import com.moecard.watch.ui.query.PlayerDetailScreen
import com.moecard.watch.ui.query.RosterScreen
import com.moecard.watch.ui.query.SearchScreen
import com.moecard.watch.ui.query.SettingsScreen
import com.moecard.watch.ui.query.SubsScreen
import com.moecard.watch.ui.watch.WatchScreen
import kotlinx.coroutines.launch

/**
 * 查询端主体导航：未登录 → 登录页；已登录 → 抽屉 + 手动返回栈。
 * 返回栈只压详情页（玩家 / 卡片），根目的地切换时清栈。
 */
sealed interface Dest {
    data object Roster : Dest
    data object Search : Dest
    data object Pie : Dest
    data object Subs : Dest
    data object Watch : Dest
    data object Settings : Dest
    data class Player(val name: String) : Dest
    data class Card(val code: Long, val name: String?) : Dest
    data class Deck(val name: String, val usage: Int) : Dest
}

private val rootDests = listOf(
    Triple(Dest.Roster, "前百玩家", Icons.Filled.EmojiEvents),
    Triple(Dest.Search, "搜索", Icons.Filled.Search),
    Triple(Dest.Pie, "前百统计", Icons.Filled.PieChart),
    Triple(Dest.Subs, "订阅管理", Icons.Filled.Notifications),
    Triple(Dest.Watch, "观战机器人", Icons.Filled.Visibility),
    Triple(Dest.Settings, "设置", Icons.Filled.Settings),
)

private fun titleOf(dest: Dest): String = when (dest) {
    Dest.Roster -> "MC 前百玩家"
    Dest.Search -> "搜索"
    Dest.Pie -> "前百统计"
    Dest.Subs -> "订阅管理"
    Dest.Watch -> "观战机器人"
    Dest.Settings -> "设置"
    is Dest.Player -> dest.name
    is Dest.Card -> dest.name ?: "卡片详情"
    is Dest.Deck -> dest.name
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AppRoot() {
    val session by QuerySession.session.collectAsStateWithLifecycle()

    if (session == null) {
        LoginScreen(onLoggedIn = { /* session flow 变化会自动切到主界面 */ })
        return
    }

    val backStack = remember { mutableStateListOf<Dest>(Dest.Roster) }
    val current = backStack.last()
    val drawerState = rememberDrawerState(initialValue = androidx.compose.material3.DrawerValue.Closed)
    val scope = rememberCoroutineScope()
    val watchVm: MainViewModel = viewModel()

    // 详情页才拦截返回键：弹一层；根页面交给系统默认行为
    BackHandler(enabled = backStack.size > 1) { backStack.removeAt(backStack.lastIndex) }

    fun open(dest: Dest) {
        // 同名玩家/同卡重复打开时去重
        backStack.removeAll { it == dest }
        backStack.add(dest)
    }

    fun switchRoot(dest: Dest) {
        backStack.clear()
        backStack.add(dest)
        scope.launch { drawerState.close() }
    }

    ModalNavigationDrawer(
        drawerState = drawerState,
        drawerContent = {
            ModalDrawerSheet {
                DrawerHeader(session!!.identity, session!!.role, session!!.writes)
                Spacer(Modifier.height(4.dp))
                rootDests.forEach { (dest, label, icon) ->
                    NavigationDrawerItem(
                        label = { Text(label) },
                        icon = { Icon(icon, contentDescription = label) },
                        selected = current == dest,
                        onClick = { switchRoot(dest) },
                        modifier = Modifier.padding(horizontal = 8.dp),
                    )
                }
                Spacer(Modifier.height(8.dp))
                Text(
                    "戏王版权局 · v1.3",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                    modifier = Modifier.padding(horizontal = 16.dp),
                )
            }
        },
    ) {
        Scaffold(
            topBar = {
                TopAppBar(
                    title = { Text(titleOf(current), maxLines = 1) },
                    navigationIcon = {
                        if (backStack.size > 1) {
                            IconButton(onClick = { backStack.removeAt(backStack.lastIndex) }) {
                                Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "返回")
                            }
                        } else {
                            IconButton(onClick = { scope.launch { drawerState.open() } }) {
                                Icon(Icons.Filled.Menu, contentDescription = "菜单")
                            }
                        }
                    },
                )
            },
        ) { padding ->
            Column(Modifier.fillMaxWidth().padding(padding)) {
                when (val dest = current) {
                    Dest.Roster -> RosterScreen(onOpenPlayer = { open(Dest.Player(it)) })
                    Dest.Search -> SearchScreen(
                        onOpenPlayer = { open(Dest.Player(it)) },
                        onOpenCard = { code, name -> open(Dest.Card(code, name)) },
                        onOpenDeck = { name, usage -> open(Dest.Deck(name, usage)) },
                    )
                    Dest.Pie -> PieScreen(
                        onOpenDeck = { name, usage -> open(Dest.Deck(name, usage)) },
                    )
                    Dest.Subs -> SubsScreen(onOpenPlayer = { open(Dest.Player(it)) })
                    Dest.Watch -> WatchScreen(watchVm)
                    Dest.Settings -> SettingsScreen(onLoggedOut = { /* session 流清空自动回登录页 */ })
                    is Dest.Player -> PlayerDetailScreen(dest.name)
                    is Dest.Card -> CardDetailScreen(dest.code, dest.name)
                    is Dest.Deck -> DeckDetailScreen(
                        deckName = dest.name,
                        usageHint = dest.usage,
                        onOpenPlayer = { open(Dest.Player(it)) },
                    )
                }
            }
        }
    }
}

@Composable
private fun DrawerHeader(identity: String, role: String, writes: Boolean) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(16.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        // QQ 官方头像 CDN；加载失败自动回落为默认占位
        AsyncImage(
            model = "https://q1.qlogo.cn/g?b=qq&nk=$identity&s=100",
            contentDescription = "头像",
            modifier = Modifier.size(48.dp).clip(CircleShape),
        )
        Spacer(Modifier.width(12.dp))
        Column {
            Text(identity.ifBlank { "已登录" }, style = MaterialTheme.typography.titleMedium)
            Row {
                Tag("角色 $role")
                if (writes) Tag("可写")
            }
        }
    }
}

@Composable
private fun Tag(text: String) {
    Surface(
        color = MaterialTheme.colorScheme.secondaryContainer,
        shape = MaterialTheme.shapes.small,
        modifier = Modifier.padding(end = 4.dp),
    ) {
        Text(
            text,
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSecondaryContainer,
            modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp),
        )
    }
}
