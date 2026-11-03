package com.moecard.watch.model

/** 一次观战任务的运行时状态，用于界面列表展示。 */
data class WatchJob(
    val id: String,
    val roomKey: String,
    val title: String,
    val players: List<String>,
    val account: String,
    val stage: String = "queued",
    val status: String = "准备中",
    val detail: String = "",
    val saved: Int = 0,
    val uploaded: Int = 0,
    val startedAt: Long = System.currentTimeMillis(),
    val finishedAt: Long? = null,
)
