package io.github.junhcha.weaver.phone

import io.github.junhcha.weaver.transport.FakeMobileServer
import io.github.junhcha.weaver.transport.HttpsMobileApi
import java.time.Instant
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class ResultClientTest {
    @get:Rule val tmp = TemporaryFolder()
    private val server = FakeMobileServer()
    private val clock = Instant.parse("2026-11-12T09:00:00Z")
    private val id = "5f19b95b9ab4aa0142341ad1"

    @After fun stop() { server.close(); assertEquals(emptyList<String>(), server.mismatches.toList()) }

    private val problems = mutableListOf<String?>()

    private fun client(live: Boolean = true, dir: java.io.File = tmp.root) =
        ResultClient({ if (live) HttpsMobileApi(FakeMobileServer.pairing(server), connectTimeoutMs = 500, readTimeoutMs = 1000) else null }, dir,
            { clock }, { problems += it })

    @Test fun documentUsesEtagAndCacheOffline() = runBlocking {
        server.serve("results.document", "results.document_not_modified")
        val client = client()
        val first = client.document(id)!!
        assertNull(first.offlineSince)
        assertEquals("네트워크", first.value.title)
        val second = client.document(id)!!
        assertEquals("304 keeps the same value", first.value, second.value)
        assertNull("a 304 confirms the copy is current", second.offlineSince)
        server.offline = true
        val offline = client.document(id)!!
        assertEquals(first.value, offline.value)
        assertEquals(clock, offline.offlineSince)
        assertNull("never fetched, never cached", client.document("other-document"))
    }

    @Test fun documentCopySurvivesARestart() = runBlocking {
        server.serve("results.document")
        client().document(id)!!
        server.offline = true
        assertEquals("네트워크", client().document(id)!!.value.title)
    }

    @Test fun missingDocumentDropsItsCopy() = runBlocking {
        server.serve("results.document")
        val client = client()
        client.document(id)!!
        server.reset("results.document")
        server.serve("results.document_not_found")
        // The not-found fixture is for another id, so the server answers 404 for this one.
        assertNull(client.document(id))
        server.offline = true
        assertNull(client.document(id))
    }

    @Test fun failedFetchKeepsCache() = runBlocking {
        server.serve("results.tree")
        val client = client()
        val fresh = client.tree()
        assertNull(fresh.offlineSince)
        assertEquals(4, fresh.value.size)
        server.failWith("/read/tree", "errors.whois_unavailable")
        val failed = client.tree()
        assertEquals(fresh.value, failed.value)
        assertEquals(clock, failed.offlineSince)
        server.clearFailures(); server.offline = true
        assertEquals(fresh.value, client.tree().value)
    }

    @Test fun noCacheAndNoServerGivesAnEmptyOfflineTree() = runBlocking {
        val result = client(live = false).tree()
        assertTrue(result.value.isEmpty())
        assertNotNull(result.offlineSince)
    }

    @Test fun evidenceIsReadEveryTimeAndNeverCached() = runBlocking {
        server.serve("results.evidence")
        val client = client()
        val evidence = client.evidence(id, "b1")!!
        assertEquals("견적 회신\n장비 목록 수령\n현장 점검", evidence.sources.single().text)
        assertEquals("card_request", evidence.threads.single().threadId)
        server.offline = true
        assertNull(client.evidence(id, "b1"))
    }

    @Test fun enabledFollowsCapabilitiesAndIsRemembered() = runBlocking {
        val client = client()
        assertTrue(client.enabled())
        server.capabilities(features = mapOf("results" to false))
        assertFalse(client.enabled())
        server.offline = true
        assertFalse("offline keeps the last answer", client.enabled())
        server.offline = false
        server.capabilities(features = mapOf("results" to true))
        assertTrue(client.enabled())
        server.offline = true
        assertTrue(client.enabled())
    }

    @Test fun referencesAreCachedPerPath() = runBlocking {
        server.serve("results.references")
        val client = client()
        val path = "회사/infra/network"
        assertEquals(0, client.references(path).value.size)
        server.offline = true
        assertEquals(clock, client.references(path).offlineSince)
    }

    @Test fun firstOpenFailureWithoutCacheIsUnavailableAndReportsTheServerCode() = runBlocking {
        server.failWith("/read/tree", "errors.whois_unavailable")
        val code = (io.github.junhcha.weaver.transport.ContractFixtures["errors.whois_unavailable"].responseBody as kotlinx.serialization.json.JsonObject)["code"]
            .let { (it as kotlinx.serialization.json.JsonPrimitive).content }
        val result = client().tree()
        assertTrue("no copy and a failed read is not an empty tree", result.unavailable)
        assertTrue(result.value.isEmpty())
        assertEquals(listOf<String?>(code), problems)
    }

    @Test fun aCachedCopyIsNeverUnavailableAndAGoodReadClearsTheProblem() = runBlocking {
        server.serve("results.tree")
        val client = client()
        assertFalse(client.tree().unavailable)
        server.failWith("/read/tree", "errors.whois_unavailable")
        assertFalse(client.tree().unavailable)
        server.clearFailures()
        client.tree()
        assertEquals(listOf(null, problems[1], null), problems)
        assertNotNull(problems[1])
    }

    @Test fun decodeFailureOnAFreshReadSurfacesAsAProblem() = runBlocking {
        server.serveDerived("results.tree") { body ->
            val items = (body as kotlinx.serialization.json.JsonObject)["items"] as kotlinx.serialization.json.JsonArray
            val broken = (items[0] as kotlinx.serialization.json.JsonObject).toMutableMap().also { it["label"] = kotlinx.serialization.json.JsonPrimitive("") }
            kotlinx.serialization.json.JsonObject(mapOf("items" to kotlinx.serialization.json.JsonArray(listOf(kotlinx.serialization.json.JsonObject(broken)) + items.drop(1))))
        }
        val result = client().tree()
        assertTrue(result.unavailable)
        assertEquals(listOf<String?>("invalid_read_response"), problems)
    }

    @Test fun unreachableServerReportsUnreachableAndNoPairingReportsNothing() = runBlocking {
        server.offline = true
        client().document(id)
        assertEquals(listOf<String?>("unreachable"), problems)
        problems.clear()
        client(live = false).tree()
        assertEquals(emptyList<String?>(), problems)
    }

    @Test fun cardsCopyOfAnotherDayIsNotPassedOffAsToday() = runBlocking {
        server.serve("results.cards_today")
        val client = client()
        val fresh = client.cards("today", "2026-11-12T09:00:00Z")
        assertNull(fresh.offlineSince)
        assertEquals(2, fresh.value.size)
        server.offline = true
        val sameDay = client.cards("today", "2026-11-12T12:00:00Z") // still 11/12 in Asia/Seoul
        assertEquals(fresh.value, sameDay.value)
        assertEquals(clock, sameDay.offlineSince)
        val nextDay = client.cards("today", "2026-11-12T16:00:00Z") // 11/13 01:00 in Asia/Seoul
        assertTrue(nextDay.unavailable)
        assertTrue(nextDay.value.isEmpty())
    }
}
