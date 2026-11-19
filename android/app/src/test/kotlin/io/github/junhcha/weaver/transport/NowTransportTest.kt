package io.github.junhcha.weaver.transport

import io.github.junhcha.weaver.phone.NowSnapshot
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.*
import org.junit.Test

class NowTransportTest {
    @Test fun nowSendsBookmarkAndFabricEpochTogetherOverPinnedTransport() {
        FakeMobileServer().use { server ->
            server.serve("request_handling.now_since_seq")
            val api = HttpsMobileApi(FakeMobileServer.pairing(server))
            val epoch = "fab_00000000000000000000000000000001"
            val snapshot = NowSnapshot.parse(api.now(0, epoch))
            assertEquals(epoch, snapshot.epoch)
            assertEquals(4L, snapshot.head)
            assertTrue(server.mismatches.isEmpty())
            assertThrows(IllegalArgumentException::class.java) { api.now(0, null) }
        }
    }

    @Test fun requestWritesAndReplayMatchServerFixtures() {
        FakeMobileServer().use { server ->
            server.serve("request_handling.deadline", "request_handling.deadline_replay", "request_handling.complete", "request_handling.undo")
            val api = HttpsMobileApi(FakeMobileServer.pairing(server), readTimeoutMs = 1000)
            for (action in listOf("deadline", "complete", "undo")) {
                val body = ContractFixtures["request_handling.$action"].requestBody!!.jsonObject
                val path = "/requests/$action"
                val response = api.edit(path, body)
                assertEquals("applied", response.getValue("state").jsonPrimitive.content)
                if (action == "deadline") {
                    assertEquals(response, api.edit(path, body))
                    assertEquals(server.bodies[0].second, server.bodies[1].second)
                }
            }
            assertTrue(server.mismatches.toString(), server.mismatches.isEmpty())
        }
    }
}
