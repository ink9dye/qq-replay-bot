package com.moecard.watch.model

/** 客户端配置。并发路数由账号数量派生：几个账号就同时观战几间房。 */
data class AppConfig(
    val server: String,
    val token: String,
    val clientName: String,
    val accounts: List<Account>,
    val keepLocal: Boolean = true,
    val autoStart: Boolean = false,
) {
    /** 并发上限 = 账号数量（至少 1，避免除零）。 */
    val capacity: Int get() = (accounts.size).coerceAtLeast(1)
}
