package io.github.junhcha.weaver.ui

import androidx.activity.ComponentActivity
import androidx.compose.material3.MaterialTheme
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import io.github.junhcha.weaver.phone.PhoneReadModel
import io.github.junhcha.weaver.phone.ResultClient
import io.github.junhcha.weaver.phone.StatusCounts
import io.github.junhcha.weaver.phone.SyntheticPhoneReadModel
import io.github.junhcha.weaver.schema.UsedIn
import io.github.junhcha.weaver.schema.UsedInKind
import io.github.junhcha.weaver.transport.FakeMobileServer
import io.github.junhcha.weaver.transport.HttpsMobileApi
import java.time.Clock
import java.time.Instant
import java.time.ZoneId
import kotlinx.serialization.json.JsonElement
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** 로그 tab with server results: one row per source, filters, paging, links out (fixture server, invented data). */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36], qualifiers = "w411dp-h800dp-xhdpi")
class LogScreenTest {
    @get:Rule val compose = createAndroidComposeRule<ComponentActivity>()
    @get:Rule val tmp = TemporaryFolder()
    private val server = FakeMobileServer()
    private val now = Instant.parse("2026-11-12T09:00:00Z")
    private val source = "src_00000000000000000000000001"
    private val usedIn = mutableListOf<UsedIn>()
    private val opened = mutableListOf<String>()

    @After fun stop() { server.close() }

    private fun client() = ResultClient({ HttpsMobileApi(FakeMobileServer.pairing(server), connectTimeoutMs = 500, readTimeoutMs = 1000) }, tmp.root) { now }

    private fun show(initial: SourceFilters = SourceFilters()) {
        server.serve("results.tree")
        val client = client()
        compose.setContent {
            MaterialTheme { WeaverTheme { SourceLog(client, { usedIn += it }, { opened += it }, ZoneId.of("UTC"), initial = initial) } }
        }
    }

    private fun has(tag: String) = compose.onAllNodes(hasTestTag(tag)).fetchSemanticsNodes().isNotEmpty()
    private fun await(tag: String) = compose.waitUntil(10_000) { has(tag) }
    private fun sourceQueries() = server.queries.filter { it.first.endsWith("/read/sources") }.map { it.second }

    @Test fun oneRowPerSourceExpandsToThreadsAndUsedIn() {
        server.serve("results.sources")
        show()
        await("source-row-$source")
        compose.onNodeWithTag("log-counts").assertIsDisplayed()
        assertEquals(1, compose.onAllNodes(hasTestTag("source-row-$source")).fetchSemanticsNodes().size)
        assertEquals(false, has("source-thread-card_request"))
        compose.onNodeWithTag("source-toggle-$source").performClick()
        for (id in listOf("card_request", "card_note", "card_visit")) compose.onNodeWithTag("source-thread-$id").assertIsDisplayed()
        compose.onNodeWithTag("source-used-$source-1").assertIsDisplayed()
        compose.onNodeWithTag("source-used-$source-0").performClick()
        assertEquals(UsedInKind.DOCUMENT, usedIn.single().kind)
        assertEquals("5f19b95b9ab4aa0142341ad1", usedIn.single().documentId)
        assertEquals("b1", usedIn.single().bulletId)
        compose.onNodeWithTag("source-used-$source-1").performClick()
        assertEquals(UsedInKind.DIGEST, usedIn.last().kind)
        assertEquals("w-2e5461320b", usedIn.last().blockId)
    }

    @Test fun filtersSendQuery() {
        server.serve("results.sources", "results.sources_filtered")
        show()
        await("source-row-$source")
        compose.onNodeWithTag("log-sphere-회사").performClick()
        compose.waitUntil(10_000) { sourceQueries().any { it["sphere"] == "회사" } }
        compose.onNodeWithTag("log-person-chip").performClick()
        await("log-pick-person-김철수")
        compose.onNodeWithTag("log-pick-person-김철수").performClick()
        compose.waitUntil(10_000) { sourceQueries().any { it["sphere"] == "회사" && it["person"] == "김철수" } }
        compose.onNodeWithTag("log-kind-chip").performClick()
        await("log-pick-kind-일정")
        compose.onNodeWithTag("log-pick-kind-일정").performClick()
        compose.waitUntil(10_000) { sourceQueries().any { it["kind"] == "일정" && it["person"] == "김철수" && it["sphere"] == "회사" } }
        // The first request carried no filter at all.
        assertEquals(emptySet<String>(), sourceQueries().first().keys - setOf("limit"))
    }

    @Test fun pagesWithCursor() {
        val next = { body: JsonElement -> JsonElementEdit.withCursor(body, "o50") }
        server.serveDerived("results.sources", next)
        server.serveDerived("results.sources_filtered", { JsonElementEdit.otherSource(it) })
        show()
        await("source-row-src_00000000000000000000000002")
        assertEquals(listOf(null, "o50"), sourceQueries().map { it["cursor"] })
        assertTrue(has("source-row-$source"))
    }

    @Test fun initialFiltersAreSent() {
        server.serve("results.sources")
        show(SourceFilters(person = "김철수"))
        await("source-row-$source")
        assertEquals("김철수", sourceQueries().first()["person"])
    }

    @Test fun sourceTextOpensFromTheRowAndNeverCompletesAnything() {
        server.serve("results.sources", "results.evidence")
        show()
        await("source-row-$source")
        compose.onNodeWithTag("source-toggle-$source").performClick()
        compose.onNodeWithTag("source-text-$source").performClick()
        await("evidence-source-$source")
        assertEquals(listOf(source), opened)
        assertEquals(0, server.requests.count { it.startsWith("POST") })
    }

    @Test fun sourceTextWithoutAnyCitingSentenceSaysSo() {
        server.serveDerived("results.sources") { JsonElementEdit.noUsedIn(it) }
        show()
        await("source-row-$source")
        compose.onNodeWithTag("source-toggle-$source").performClick()
        compose.onNodeWithTag("source-text-$source").performClick()
        await("source-text-unavailable")
    }

    @Test @Config(qualifiers = "w900dp-h600dp")
    fun wideShowsTheSourceBesideTheList() {
        server.serve("results.sources")
        show()
        await("source-row-$source")
        compose.onNodeWithTag("source-toggle-$source").performClick()
        compose.onNodeWithTag("source-text-$source").performClick()
        await("source-text-pane")
        compose.onNodeWithTag("source-list").assertIsDisplayed()
    }

    @Test fun documentLinkFiltersByTheSphereOfItsSubjectAndOthersByPerson() {
        val nodes = ResultFixtures.tree
        assertEquals(SourceFilters(sphere = "회사"), logFiltersFor("회사/infra/network", nodes))
        assertEquals(SourceFilters(person = "김철수"), logFiltersFor("김철수", nodes))
    }

    @Test fun areaLinkFindsItsNodeByPathLabelOrLastSegment() {
        val nodes = ResultFixtures.tree
        assertEquals("회사/infra/network", areaNodeFor("회사/infra/network", nodes)?.path)
        assertEquals("회사/infra/storage", areaNodeFor("Storage", nodes)?.path)
        assertEquals("회사/infra", areaNodeFor("infra", nodes)?.path)
        assertEquals(null, areaNodeFor("없는 영역", nodes))
    }

    @Test fun digestLinkReadsAsItsDate() {
        assertEquals("11/12 Digest", usedInLabel(UsedIn(UsedInKind.DIGEST, "d1", "2026-11-12 (목)", null, "w-1")))
        assertEquals("네트워크", usedInLabel(UsedIn(UsedInKind.DOCUMENT, "d2", "네트워크", "b1", null)))
    }

    @Test fun documentLogLinkOpensTheFilteredLogTab() {
        server.serve("results.tree", "results.document", "results.sources")
        server.serveDerived("results.references", ResultFixtures::withReference)
        val results = client()
        val model = object : PhoneReadModel by SyntheticPhoneReadModel(Clock.fixed(now, ZoneId.of("UTC"))) {
            override val results: ResultClient = results
        }
        compose.setContent {
            MaterialTheme { WeaverTheme { PhoneShell(model, StatusCounts(0, 0, 0), { now }, {}, {}, initialTab = PhoneTab.AREAS) } }
        }
        await("area-node-회사/infra/network")
        compose.onNodeWithTag("area-node-회사/infra/network").performClick()
        await("document-log-link")
        compose.onNodeWithTag("document-log-link").performClick()
        await("source-row-$source")
        assertEquals("회사", sourceQueries().last()["sphere"])
    }
}

/** Small edits of the recorded `sources` answer (cursor, another source id). */
private object JsonElementEdit {
    fun withCursor(body: JsonElement, cursor: String): JsonElement =
        kotlinx.serialization.json.Json.parseToJsonElement(body.toString().replace("\"next_cursor\":null", "\"next_cursor\":\"$cursor\""))
    fun noUsedIn(body: JsonElement): JsonElement = kotlinx.serialization.json.Json.parseToJsonElement(
        Regex("\"used_in\":\\[.*?\\]\\}\\],\"next_cursor\"").replace(body.toString(), "\"used_in\":[]}],\"next_cursor\""))
    fun otherSource(body: JsonElement): JsonElement =
        kotlinx.serialization.json.Json.parseToJsonElement(body.toString().replace("src_00000000000000000000000001", "src_00000000000000000000000002"))
}
