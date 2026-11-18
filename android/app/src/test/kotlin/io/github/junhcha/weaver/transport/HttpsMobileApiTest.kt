package io.github.junhcha.weaver.transport

import kotlinx.serialization.json.JsonPrimitive
import org.junit.After
import org.junit.Assert.*
import org.junit.Test

class HttpsMobileApiTest {
    private val servers = mutableListOf<FakeMobileServer>()
    private fun server(leaf: String = "ip:127.0.0.1", pki: TestPki = TestPki.default) = FakeMobileServer(leaf = leaf, pki = pki).also { servers += it }
    @After fun stop() = servers.forEach { it.close() }

    private fun api(server: FakeMobileServer, pairing: Pairing = FakeMobileServer.pairing(server)) =
        HttpsMobileApi(pairing, connectTimeoutMs = 2000, readTimeoutMs = 2000)

    @Test fun talksToTheServerWhoseCertificateChainsToThePairedCa() {
        val s = server()
        assertEquals("srv-test", (api(s).capabilities()["server_id"] as JsonPrimitive).content)
        assertEquals("Bearer ${s.token}", s.seenAuth.single())
    }

    @Test fun aReissuedLeafUnderTheSameCaNeedsNoNewPairing() {
        val first = server(); val pairing = FakeMobileServer.pairing(first)
        first.close()
        val second = server("ip:127.0.0.1,dns:weaver.local") // a new leaf with another key, same CA
        api(second, pairing.copy(baseUrl = second.url)).capabilities()
    }

    @Test fun refusesACertificateFromAnotherCa() {
        val other = TestPki.create()
        val s = server(pki = other)
        assertThrows(PinMismatchException::class.java) { api(s, FakeMobileServer.pairing(s, pki = TestPki.default)).capabilities() }
        assertTrue("no request may reach the handler", s.requests.isEmpty())
    }

    @Test fun refusesAValidLeafWhoseNamesDoNotContainTheServerAddress() {
        val s = server("ip:10.9.9.9")
        assertThrows(PinMismatchException::class.java) { api(s).capabilities() }
        val noSan = server("")
        assertThrows(PinMismatchException::class.java) { api(noSan).capabilities() }
    }

    @Test fun neverSpeaksPlainHttp() {
        val s = server()
        assertThrows(IllegalArgumentException::class.java) {
            HttpsMobileApi(FakeMobileServer.pairing(s).copy(baseUrl = "http://127.0.0.1:${s.port}"))
        }
    }

    @Test fun wrongTokenIsA401WithTheFixedCode() {
        val s = server()
        val error = assertThrows(ApiException::class.java) { api(s, FakeMobileServer.pairing(s, token = "x".repeat(43))).capabilities() }
        assertEquals(401, error.status)
        assertEquals("token_invalid", error.code)
        assertFalse(error.retryable)
    }

    @Test fun anUnreachableServerIsWaitingNotAnError() {
        val s = server()
        val pairing = FakeMobileServer.pairing(s)
        s.close()
        assertThrows(UnreachableException::class.java) {
            HttpsMobileApi(pairing, connectTimeoutMs = 500, readTimeoutMs = 500).capabilities()
        }
    }

    @Test fun serverErrorsCarryRetryability() {
        val s = server()
        s.fail["/capabilities"] = 503
        s.failCodes["/capabilities"] = "whois_unavailable"
        val error = assertThrows(ApiException::class.java) { api(s).capabilities() }
        assertTrue(error.retryable)
        assertEquals("whois_unavailable", error.code)
    }

    @Test fun anOversizedResponseIsPagedSmallerInsteadOfFailing() {
        val s = server()
        val big = "x".repeat(600_000)
        s.threads = (1..10).map { """{"thread_id":"t$it","pad":"$big"}""" }
        s.pageSize = 10 // the server would send everything in one 6 MB page
        s.maxItemsPerResponse = 10
        val items = Paging.all(500) { cursor, limit -> api(s).threads(cursor, limit) }
        assertEquals(10, items.size)
        assertTrue("limit was halved after the first too-large page", s.requests.count { it.contains("threads") } > 1)
    }
}
