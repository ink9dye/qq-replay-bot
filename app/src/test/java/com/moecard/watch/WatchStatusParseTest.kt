package com.moecard.watch

import com.moecard.watch.model.parseWatchStatus
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class WatchStatusParseTest {

    @Test
    fun parsesWorkersAndSortsOnlineFirst() {
        val raw = """
            {
              "ok": true,
              "pending": 2,
              "claimed": 5,
              "workers": {
                "pc-2": {"online": false, "active": 0, "capacity": 2, "ip": "10.0.0.2", "seen": 1762470000},
                "android-1": {"online": true, "active": 1, "capacity": 3, "ip": "10.0.0.1", "seen": 1762473600},
                "pc-1": {"online": true, "active": 2, "capacity": 2}
              }
            }
        """.trimIndent()

        val status = parseWatchStatus(JSONObject(raw))

        assertEquals(3, status.devices.size)
        assertEquals(2, status.onlineDevices.size)
        assertEquals(1, status.offlineDevices.size)
        assertEquals(2, status.pending)
        assertEquals(5, status.claimed)
        // 在线排前；离线里只有 pc-2
        assertEquals("android-1", status.devices[0].name)
        assertEquals("pc-1", status.devices[1].name)
        assertEquals("pc-2", status.devices[2].name)
        // 缺省字段要有安全兜底
        assertEquals("", status.devices[1].ip)
        assertEquals(0.0, status.devices[1].seenAt, 0.0)
        assertEquals(1, status.devices[0].active)
        assertEquals(3, status.devices[0].capacity)
        assertTrue(status.devices[0].online)
    }

    @Test
    fun missingWorkersObjectYieldsEmptyList() {
        val status = parseWatchStatus(JSONObject("""{"pending":0,"claimed":0}"""))
        assertEquals(0, status.devices.size)
    }
}
