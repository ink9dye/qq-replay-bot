package com.moecard.watch.data

import android.Manifest
import android.content.ContentValues
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import androidx.annotation.RequiresApi
import androidx.core.content.ContextCompat
import java.io.File

/**
 * 把文件存进系统「下载」目录下的应用子目录：下载/戏王版权局/录像、下载/戏王版权局/卡组。
 *
 *  - Android 10+：MediaStore.Downloads + RELATIVE_PATH，不需要任何权限；先标 IS_PENDING
 *    写完再放出来，写到一半失败会把半截记录删掉，不会在下载目录留下坏文件；重名由系统自动加序号。
 *  - Android 8–9：写公共 Download 目录，需要 WRITE_EXTERNAL_STORAGE（Manifest 里 maxSdk=28）；
 *    没授权时退回应用自己的外部目录，并在返回的位置里如实写明，不再谎称“已保存到下载”。
 */
object DownloadSaver {
    const val ROOT_DIR = "戏王版权局"

    enum class Kind(val folder: String) {
        REPLAY("录像"),
        DECK("卡组"),
    }

    /** 保存结果：给用户看的位置、是否真的进了系统下载目录。 */
    data class Saved(val displayPath: String, val inPublicDownloads: Boolean)

    /** Android 8–9 上写公共下载目录是否还缺权限（Android 10+ 恒为 false）。 */
    fun needsLegacyPermission(context: Context): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.Q &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.WRITE_EXTERNAL_STORAGE) !=
            PackageManager.PERMISSION_GRANTED

    fun save(context: Context, fileName: String, bytes: ByteArray, kind: Kind): Saved {
        val name = sanitize(fileName)
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            saveMediaStore(context, name, bytes, kind)
        } else {
            saveLegacy(context, name, bytes, kind)
        }
    }

    @RequiresApi(Build.VERSION_CODES.Q)
    private fun saveMediaStore(context: Context, name: String, bytes: ByteArray, kind: Kind): Saved {
        val resolver = context.contentResolver
        val relative = "${Environment.DIRECTORY_DOWNLOADS}/$ROOT_DIR/${kind.folder}"
        val values = ContentValues().apply {
            put(MediaStore.Downloads.DISPLAY_NAME, name)
            // octet-stream：系统不会因为 MIME 给 .yrp/.ydk 追加别的扩展名
            put(MediaStore.Downloads.MIME_TYPE, "application/octet-stream")
            put(MediaStore.Downloads.RELATIVE_PATH, relative)
            put(MediaStore.Downloads.IS_PENDING, 1)
        }
        val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
            ?: throw RuntimeException("无法在下载目录创建文件")
        try {
            val out = resolver.openOutputStream(uri) ?: throw RuntimeException("无法写入下载目录")
            out.use { it.write(bytes) }
            resolver.update(uri, ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }, null, null)
        } catch (e: Exception) {
            try {
                resolver.delete(uri, null, null)
            } catch (_: Exception) {
            }
            throw e
        }
        // 重名时系统会改成 “xxx (1).yrp”，把真实文件名读回来给用户看
        val finalName = try {
            resolver.query(uri, arrayOf(MediaStore.Downloads.DISPLAY_NAME), null, null, null)?.use { c ->
                if (c.moveToFirst()) c.getString(0) else null
            }
        } catch (_: Exception) {
            null
        } ?: name
        return Saved("下载/$ROOT_DIR/${kind.folder}/$finalName", inPublicDownloads = true)
    }

    @Suppress("DEPRECATION")
    private fun saveLegacy(context: Context, name: String, bytes: ByteArray, kind: Kind): Saved {
        if (!needsLegacyPermission(context)) {
            try {
                val dir = File(
                    Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS),
                    "$ROOT_DIR/${kind.folder}",
                )
                if (!dir.exists()) dir.mkdirs()
                val f = uniqueFile(dir, name)
                f.writeBytes(bytes)
                return Saved("下载/$ROOT_DIR/${kind.folder}/${f.name}", inPublicDownloads = true)
            } catch (_: Exception) {
                // 存储不可用等极端情况：落到下面的应用目录
            }
        }
        val base = context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS) ?: context.filesDir
        val dir = File(base, kind.folder)
        if (!dir.exists()) dir.mkdirs()
        val f = uniqueFile(dir, name)
        f.writeBytes(bytes)
        return Saved("${f.absolutePath}（未授予存储权限，存在应用目录）", inPublicDownloads = false)
    }

    /** 同名文件已存在时加 (2)、(3)… 而不是覆盖。 */
    private fun uniqueFile(dir: File, name: String): File {
        var f = File(dir, name)
        if (!f.exists()) return f
        val dot = name.lastIndexOf('.')
        val stem = if (dot > 0) name.substring(0, dot) else name
        val ext = if (dot > 0) name.substring(dot) else ""
        var i = 2
        while (f.exists()) {
            f = File(dir, "$stem ($i)$ext")
            i++
        }
        return f
    }

    private fun sanitize(name: String): String =
        name.replace(Regex("""[\\/:*?"<>|\r\n\t]"""), "_").trim().ifBlank { "file" }
}
