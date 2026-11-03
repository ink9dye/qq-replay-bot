package com.moecard.watch.model

/** 一条运行日志。level: info / success / warn / error。 */
data class LogEntry(
    val time: Long,
    val message: String,
    val level: String = "info",
)
