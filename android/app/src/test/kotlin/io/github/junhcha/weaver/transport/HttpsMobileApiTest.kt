package io.github.junhcha.weaver.transport

import org.junit.After
import org.junit.Assert.*
import org.junit.Test

class HttpsMobileApiTest {
    private val server = FakeMobileServer()
    @After fun stop() = server.close()

    private fun api(fingerprint: String = server.fingerprint, token: String = server.token) =
        HttpsMobileApi(FakeMobileServer.pairing(server, fingerprint, token), connectTimeoutMs = 2000, readTimeoutMs = 2000)

    @Test fun talksToTheServerWhosePinnedCertificateMatches() {
        assertEquals("srv-test", (api().capabilities()["server_id"] as kotlinx.serialization.json.JsonPrimitive).content)
        assertEquals("Bearer ${server.token}", server.seenAuth.single())
    }

    @Test fun refusesACertificateThatIsNotThePairedOne() {
        assertThrows(PinMismatchException::class.java) { api(fingerprint = "00".repeat(32)).capabilities() }
        assertTrue("no request may reach the handler", server.requests.isEmpty())
    }

    @Test fun neverSpeaksPlainHttp() {
        assertThrows(IllegalArgumentException::class.java) {
            HttpsMobileApi(Pairing("http://127.0.0.1:${server.port}", server.fingerprint, server.token))
        }
    }

    @Test fun wrongTokenIsA401WithTheFixedCode() {
        val error = assertThrows(ApiException::class.java) { api(token = "x".repeat(43)).capabilities() }
        assertEquals(401, error.status)
        assertEquals("token_invalid", error.code)
        assertFalse(error.retryable)
    }

    @Test fun anUnreachableServerIsWaitingNotAnError() {
        val port = server.port
        val pairing = FakeMobileServer.pairing(server)
        server.close()
        assertThrows(UnreachableException::class.java) {
            HttpsMobileApi(pairing.copy(baseUrl = "https://127.0.0.1:$port"), connectTimeoutMs = 500, readTimeoutMs = 500).capabilities()
        }
    }

    @Test fun serverErrorsCarryRetryabilityAndNeverFollowRedirects() {
        server.fail["/capabilities"] = 503
        val error = assertThrows(ApiException::class.java) { api().capabilities() }
        assertTrue(error.retryable)
        assertEquals(503, error.status)
    }
}
