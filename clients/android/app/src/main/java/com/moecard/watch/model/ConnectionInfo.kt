package com.moecard.watch.model

/** 服务端 /watch-api/status 返回的连接概览。 */
data class ConnectionInfo(
    val onlineWorkers: Int,
    val pending: Int,
    val claimed: Int,
)
