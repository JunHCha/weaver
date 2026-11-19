package io.github.junhcha.weaver.transport

import kotlinx.serialization.json.JsonPrimitive
import org.junit.After
import org.junit.Assert.*
import org.junit.Test

class HttpsMobileApiTest {
    private val servers = mutableListOf<FakeMobileServer>()
    private fun server(leaf: String = "ip:127.0.0.1", pki: TestPki = TestPki.default) =
        FakeMobileServer(leaf = leaf, pki = pki).also { it.serve("capabilities.capabilities"); servers += it }
    @After fun stop() = servers.forEach { it.close() }

    private fun api(server: FakeMobileServer, pairing: Pairing = FakeMobileServer.pairing(server)) =
        HttpsMobileApi(pairing, connectTimeoutMs = 2000, readTimeoutMs = 2000)

    @Test fun talksToTheServerWhoseCertificateChainsToThePairedCa() {
        val s = server()
        assertEquals(s.serverId, (api(s).capabilities()["server_id"] as JsonPrimitive).content)
        assertEquals("Bearer ${s.token}", s.seenAuth.single())
    }

    @Test fun aReissuedLeafUnderTheSameCaNeedsNoNewPairing() {
        val first = server(); val pairing = FakeMobileServer.pairing(first)
        first.close()
        val second = server("ip:127.0.0.1,dns:weaver.local") // a new leaf with another key, same CA
        api(second, pairing.copy(baseUrl = second.url)).capabilities()
    }

    @Test fun refusesACertificateFromAnotherCa() {
        val s = server(pki = TestPki.create())
        assertThrows(PinMismatchException::class.java) { api(s, FakeMobileServer.pairing(s, pki = TestPki.default)).capabilities() }
        assertTrue("no request may reach the handler", s.requests.isEmpty())
    }

    @Test fun refusesAValidLeafWhoseNamesDoNotContainTheServerAddress() {
        assertThrows(PinMismatchException::class.java) { api(server("ip:10.9.9.9")).capabilities() }
        assertThrows(PinMismatchException::class.java) { api(server("")).capabilities() }
    }

    @Test fun neverSpeaksPlainHttp() {
        val s = server()
        assertThrows(IllegalArgumentException::class.java) {
            HttpsMobileApi(FakeMobileServer.pairing(s).copy(baseUrl = "http://127.0.0.1:${s.port}"))
        }
    }

    @Test fun errorFixturesMapToTheFixedCodesAndRetryability() {
        val s = server()
        mapOf(
            "errors.invalid_token" to (401 to false), "errors.node_mismatch" to (403 to false), "errors.peer_unverified" to (403 to false),
            "errors.token_revoked" to (401 to false), "errors.whois_unavailable" to (503 to true),
        ).forEach { (name, expected) ->
            s.clearFailures(); s.failWith("/capabilities", name)
            val error = assertThrows(name, ApiException::class.java) { api(s).capabilities() }
            assertEquals(name, expected.first, error.status); assertEquals(name, expected.second, error.retryable)
            assertEquals(name.removePrefix("errors."), error.code)
        }
    }

    @Test fun aWrongTokenIsTheInvalidTokenFixture() {
        val s = server()
        val error = assertThrows(ApiException::class.java) { api(s, FakeMobileServer.pairing(s, token = "wvr_" + "9".repeat(43))).capabilities() }
        assertEquals(401, error.status); assertEquals("invalid_token", error.code)
    }

    @Test fun anUnreachableServerIsWaitingNotAnError() {
        val s = server()
        val pairing = FakeMobileServer.pairing(s)
        s.close()
        assertThrows(UnreachableException::class.java) { HttpsMobileApi(pairing, connectTimeoutMs = 500, readTimeoutMs = 500).capabilities() }
    }

    @Test fun anOversizedPageIsRequestedAgainWithAHalvedLimit() {
        val limits = mutableListOf<Int>()
        val items = Paging.all(500) { cursor, limit ->
            limits += limit
            if (limit > 100) throw ApiException(200, "response_too_large", false)
            Page(listOf(JsonPrimitive("a$limit")), if (cursor == null) "c1" else null)
        }
        assertEquals(listOf(500, 250, 125, 62, 62), limits)
        assertEquals(2, items.size)
        // The limit never drops below the floor: a page that is still too large after that is a real failure.
        assertThrows(ApiException::class.java) { Paging.all(10) { _, _ -> throw ApiException(200, "response_too_large", false) } }
    }
}
