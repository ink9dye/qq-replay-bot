package com.moecard.watch.net

/** 令牌无效 / 登录过期类错误，调用方据此提示用户重新填写。 */
class AuthException(message: String) : RuntimeException(message)
