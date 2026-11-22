package com.moecard.watch.ui.query

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import androidx.core.content.FileProvider
import com.moecard.watch.query.model.BuildData
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File

/** YGOMobile 包名：国内版带 .CN，国际版不带。 */
private val YGO_PACKAGES = listOf("cn.garymb.ygomobile.CN", "cn.garymb.ygomobile")

/**
 * 构筑 → .ydk 文本。段落格式照 YGOMobile 自己导出的文件：
 * `#main` / `#extra` 段是裸卡号（每张一行），副卡组段以 `!side` 开头。
 */
fun buildYdk(build: BuildData): String {
    val sb = StringBuilder("#created by MoeCardQuery\n#main\n")
    fun append(partition: String) {
        build.cards.asSequence()
            .filter { it.partition == partition && it.code > 0L && it.copies > 0 }
            .forEach { c -> repeat(c.copies) { sb.append(c.code).append('\n') } }
    }
    append("main")
    sb.append("#extra\n")
    append("extra")
    sb.append("!side\n")
    append("side")
    return sb.toString()
}

/**
 * 把构筑写成 .ydk 并交给 YGOMobile 打开。
 *
 * 不能直接写它的卡组目录：`Android/data/cn.garymb.ygomobile.CN/files/ygocore/deck`
 * 属于别的应用，Android 11+ 即使拿到存储权限也会 Permission denied（已实测）。
 * 所以走它注册的 VIEW intent（它接受 content URI + `.*.ydk` 路径），
 * 打开后在 YGOMobile 里点「保存」即进入它的卡组列表。
 * 没装 YGOMobile 时退回把 .ydk 存到系统「下载」目录。
 */
suspend fun openYdkInYgomobile(ctx: Context, deckName: String, build: BuildData): String = try {
    val file = withContext(Dispatchers.IO) {
        val dir = File(ctx.cacheDir, "export").apply { mkdirs() }
        File(dir, safeFileName(deckName) + ".ydk").apply { writeText(buildYdk(build)) }
    }
    val uri = FileProvider.getUriForFile(ctx, "${ctx.packageName}.fileprovider", file)
    val view = Intent(Intent.ACTION_VIEW).apply {
        setDataAndType(uri, "*/*")
        // CLEAR_TASK：YGOMobile 已在前台时只把任务提到前面、不重读新 intent，
        // 会停在上一个卡组；清掉它的任务让它按新 URI 重新 onCreate。
        addFlags(
            Intent.FLAG_GRANT_READ_URI_PERMISSION or
                Intent.FLAG_ACTIVITY_NEW_TASK or
                Intent.FLAG_ACTIVITY_CLEAR_TASK
        )
    }
    var opened = false
    for (pkg in YGO_PACKAGES) {
        try {
            ctx.startActivity(Intent(view).setPackage(pkg))
            opened = true
            break
        } catch (_: ActivityNotFoundException) {
            // 换下一个包名
        }
    }
    if (opened) {
        "已在 YGOMobile 打开「${file.nameWithoutExtension}」，点「保存」即可加入卡组列表"
    } else {
        val saved = withContext(Dispatchers.IO) { saveToDownloads(ctx, file.name, file.readBytes()) }
        "未检测到 YGOMobile，已保存到 $saved"
    }
} catch (e: Exception) {
    "导入失败：${e.message ?: "未知错误"}"
}

/** 卡组名当文件名：去掉 `/\:*?"<>|` 等非法字符，空名回退 deck。 */
private fun safeFileName(name: String): String =
    name.replace(Regex("""[\\/:*?"<>|\r\n\t]"""), "_").trim().take(40).ifBlank { "deck" }

/**
 * 把单局录像 (.yrp) 交给 YGOMobile 打开。它的 MainActivity 注册了 .yrp 的 VIEW
 * （manifest 里 content/file scheme + `.*\.yrp` 路径），打开后可直接回放/另存；
 * 没装 YGOMobile 时退回存到系统「下载」目录。
 */
suspend fun openYrpInYgomobile(ctx: Context, fileName: String, bytes: ByteArray): String = try {
    val name = fileName.ifBlank { "replay" }.let { if (it.endsWith(".yrp")) it else "$it.yrp" }
    val file = withContext(Dispatchers.IO) {
        val dir = File(ctx.cacheDir, "export").apply { mkdirs() }
        File(dir, safeFileName(name.removeSuffix(".yrp")) + ".yrp").apply { writeBytes(bytes) }
    }
    val uri = FileProvider.getUriForFile(ctx, "${ctx.packageName}.fileprovider", file)
    val view = Intent(Intent.ACTION_VIEW).apply {
        setDataAndType(uri, "*/*")
        addFlags(
            Intent.FLAG_GRANT_READ_URI_PERMISSION or
                Intent.FLAG_ACTIVITY_NEW_TASK or
                Intent.FLAG_ACTIVITY_CLEAR_TASK
        )
    }
    var opened = false
    for (pkg in YGO_PACKAGES) {
        try {
            ctx.startActivity(Intent(view).setPackage(pkg))
            opened = true
            break
        } catch (_: ActivityNotFoundException) {
            // 换下一个包名
        }
    }
    if (opened) {
        "已在 YGOMobile 打开「${file.name}」"
    } else {
        val saved = withContext(Dispatchers.IO) { saveToDownloads(ctx, file.name, file.readBytes()) }
        "未检测到 YGOMobile，已保存到 $saved"
    }
} catch (e: Exception) {
    "导入失败：${e.message ?: "未知错误"}"
}
