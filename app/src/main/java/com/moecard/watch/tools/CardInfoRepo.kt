package com.moecard.watch.tools

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit

/**
 * 计算器的卡片数据：联网按需查百鸽（ygocdb），查过的存在应用私有目录，下次离线也能用。
 *
 * 只查当前卡组里出现的卡号，不内置全卡库，App 体积不变。
 */
object CardInfoRepo {
    private const val CACHE_FILE = "tool_card_cache.json"
    private const val MAX_PARALLEL = 4

    private val memory = ConcurrentHashMap<Long, CardInfo>()
    @Volatile private var diskLoaded = false

    private val client = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    data class LoadResult(val cards: Map<Long, CardInfo>, val missing: List<Long>)

    /**
     * 取一批卡的数据：先内存、再本地缓存、最后联网。[onProgress] 报告（已完成, 总数）。
     * 查不到的卡号放进 [LoadResult.missing]，不抛异常、不中断整个卡组。
     */
    suspend fun load(
        context: Context,
        codes: Collection<Long>,
        onProgress: (Int, Int) -> Unit = { _, _ -> },
    ): LoadResult = withContext(Dispatchers.IO) {
        ensureDisk(context)
        val unique = codes.filter { it > 0 }.distinct()
        val todo = unique.filter { !memory.containsKey(it) }
        var done = unique.size - todo.size
        onProgress(done, unique.size)
        if (todo.isNotEmpty()) {
            val gate = Semaphore(MAX_PARALLEL)
            coroutineScope {
                todo.map { code ->
                    async {
                        gate.withPermit {
                            fetch(code)?.let { memory[code] = it }
                        }
                        synchronized(this@CardInfoRepo) {
                            done += 1
                            onProgress(done, unique.size)
                        }
                    }
                }.awaitAll()
            }
            saveDisk(context)
        }
        val found = LinkedHashMap<Long, CardInfo>()
        val missing = ArrayList<Long>()
        for (code in unique) {
            val c = memory[code]
            if (c != null) found[code] = c else missing.add(code)
        }
        LoadResult(found, missing)
    }

    private fun fetch(code: Long): CardInfo? {
        val req = Request.Builder()
            .url("https://ygocdb.com/api/v0/card/$code")
            .header("User-Agent", "MoeCardQuery/2.2")
            .get()
            .build()
        repeat(2) {
            try {
                client.newCall(req).execute().use { resp ->
                    if (resp.code == 404) return null
                    if (resp.code == 200) {
                        val raw = resp.body?.string().orEmpty()
                        if (raw.isBlank()) return null
                        return CardInfo.fromYgocdb(JSONObject(raw), code)?.copy(code = code)
                    }
                }
            } catch (_: Exception) {
                // 网络抖动：再试一次
            }
        }
        return null
    }

    private fun cacheFile(context: Context) = File(context.applicationContext.filesDir, CACHE_FILE)

    @Synchronized
    private fun ensureDisk(context: Context) {
        if (diskLoaded) return
        diskLoaded = true
        try {
            val f = cacheFile(context)
            if (!f.isFile) return
            val arr = JSONArray(f.readText())
            for (i in 0 until arr.length()) {
                val c = arr.optJSONObject(i)?.let { CardInfo.fromCacheJson(it) } ?: continue
                memory.putIfAbsent(c.code, c)
            }
        } catch (_: Exception) {
            // 缓存坏了就当没有，重新联网查
        }
    }

    @Synchronized
    private fun saveDisk(context: Context) {
        try {
            val arr = JSONArray()
            memory.values.forEach { arr.put(CardInfo.toCacheJson(it)) }
            val f = cacheFile(context)
            val tmp = File(f.parentFile, "$CACHE_FILE.tmp")
            tmp.writeText(arr.toString())
            if (!tmp.renameTo(f)) {
                f.delete()
                tmp.renameTo(f)
            }
        } catch (_: Exception) {
        }
    }
}
