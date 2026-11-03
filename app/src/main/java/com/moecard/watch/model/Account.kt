package com.moecard.watch.model

/** 一个萌卡账号：账号名 + 密码。一个账号同时在场观战一间房。 */
data class Account(
    val name: String,
    val password: String,
)
