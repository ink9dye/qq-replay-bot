package com.moecard.watch.model

import org.json.JSONObject

/** 服务器派发的对局房间信息（由 claim 返回的 job 解析）。 */
data class RoomInfo(
    val key: String,
    val title: String,
    val joinId: String?,
    val players: List<String>,
    val seats: Map<Int, String>,
    val started: Boolean,
    val host: String,
    val port: Int,
    val version: Int,
    val focus: List<String>,
) {
    companion object {
        fun fromJob(job: JSONObject): RoomInfo {
            val room = job.optJSONObject("room") ?: JSONObject()
            val server = job.optJSONObject("server") ?: JSONObject()
            val players = mutableListOf<String>()
            room.optJSONArray("players")?.let { a ->
                for (i in 0 until a.length()) players.add(a.optString(i))
            }
            val seats = mutableMapOf<Int, String>()
            room.optJSONObject("seats")?.let { s ->
                s.keys().forEach { k -> s.optString(k).takeIf { it.isNotEmpty() }?.let { seats[k.toIntOrNull() ?: -1] = it } }
            }
            val joinId = room.optString("join_id").takeIf { it.isNotEmpty() }
            return RoomInfo(
                key = room.optString("key").ifEmpty { job.optString("id") },
                title = room.optString("title").ifEmpty { joinId ?: "未知房间" },
                joinId = joinId,
                players = players,
                seats = seats,
                started = room.optBoolean("started", false),
                host = server.optString("host").ifEmpty { "tiramisu.moenext.com" },
                port = server.optInt("port", 8911),
                version = server.optInt("version", 0x1362),
                focus = run {
                    val l = mutableListOf<String>()
                    job.optJSONArray("focus")?.let { a -> for (i in 0 until a.length()) l.add(a.optString(i)) }
                    l
                },
            )
        }
    }
}
