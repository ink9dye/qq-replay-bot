package com.moecard.watch.model

import org.json.JSONObject

/** 服务端 /watch-api/status 返回的连接概览。 */
data class ConnectionInfo(
    val onlineWorkers: Int,
    val pending: Int,
    val claimed: Int,
)

/** 单台观战终端；字段来自服务端 status 的 workers[name]。 */
data class WatchDevice(
    val name: String,
    val ip: String,
    val online: Boolean,
    val active: Int,
    val capacity: Int,
    /** 最近一次心跳时间（epoch 秒），0 表示服务端未返回。 */
    val seenAt: Double,
)

/** 服务端设备看板快照：在线终端 + 任务板概览。 */
data class WatchStatus(
    val devices: List<WatchDevice>,
    val pending: Int,
    val claimed: Int,
) {
    val onlineDevices: List<WatchDevice> get() = devices.filter { it.online }
    val offlineDevices: List<WatchDevice> get() = devices.filter { !it.online }
}

/** 解析 /watch-api/status；workers 是 { 名称: {online, active, capacity, ip, seen} }。 */
fun parseWatchStatus(root: JSONObject): WatchStatus {
    val workers = root.optJSONObject("workers") ?: JSONObject()
    val devices = ArrayList<WatchDevice>()
    workers.keys().forEach { name ->
        val info = workers.optJSONObject(name) ?: return@forEach
        devices.add(
            WatchDevice(
                name = name,
                ip = info.optString("ip", ""),
                online = info.optBoolean("online", false),
                active = info.optInt("active", 0).coerceAtLeast(0),
                capacity = info.optInt("capacity", 1).coerceAtLeast(1),
                seenAt = info.optDouble("seen", 0.0),
            )
        )
    }
    // 在线优先，其次按名称，方便列表稳定展示。
    devices.sortWith(compareByDescending<WatchDevice> { it.online }.thenBy { it.name })
    return WatchStatus(
        devices = devices,
        pending = root.optInt("pending", 0),
        claimed = root.optInt("claimed", 0),
    )
}
