package io.github.junhcha.weaver.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.hasAnyAncestor
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.v2.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import io.github.junhcha.weaver.phone.CompleteResult
import io.github.junhcha.weaver.phone.DoneStore
import io.github.junhcha.weaver.phone.LinkTarget
import io.github.junhcha.weaver.phone.PendingRequest
import io.github.junhcha.weaver.phone.PhoneReadModel
import io.github.junhcha.weaver.phone.ResultClient
import io.github.junhcha.weaver.phone.StatusCounts
import io.github.junhcha.weaver.phone.SyntheticPhoneReadModel
import io.github.junhcha.weaver.schema.LinkKind
import io.github.junhcha.weaver.schema.ResultCard
import io.github.junhcha.weaver.schema.TaskItem
import io.github.junhcha.weaver.transport.FakeMobileServer
import io.github.junhcha.weaver.transport.HttpsMobileApi
import java.io.File
import java.time.Clock
import java.time.Instant
import java.time.ZoneId
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** 지금 tab built from results: my open tasks, today's incoming cards and the days around (fixture server, invented data). */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36], qualifiers = "w411dp-h1600dp-xhdpi")
class NowResultsTest {
    @get:Rule val compose = createComposeRule()
    @get:Rule val tmp = TemporaryFolder()
    private val server = FakeMobileServer()
    private val now = Instant.parse("2026-11-12T09:00:00Z")
    private val zone = ZoneId.of("Asia/Seoul")
    private var reload by mutableIntStateOf(0)

    private fun applied() = CompleteResult.Done(PendingRequest("e", "c", "/requests/complete", JsonObject(emptyMap()), "applied", null, null))

    @After fun stop() { server.close(); assertEquals(emptyList<String>(), server.mismatches.toList()) }

    private fun results() = ResultClient({ HttpsMobileApi(FakeMobileServer.pairing(server), connectTimeoutMs = 500, readTimeoutMs = 1000) },
        File(tmp.root, "results")) { now }

    private fun has(tag: String) = compose.onAllNodes(hasTestTag(tag)).fetchSemanticsNodes().isNotEmpty()
    private fun await(tag: String) = compose.waitUntil(10_000) { has(tag) }
    private fun gone(tag: String) = compose.waitUntil(10_000) { !has(tag) }

    /** What PhoneShell does around [NowResultsContent]: the check flow, then a re-read of the tasks. */
    private fun harness(done: DoneStore, complete: suspend (String, Long) -> CompleteResult, opened: MutableList<ResultCard> = mutableListOf()) {
        val results = results()
        compose.setContent { MaterialTheme { WeaverTheme {
            val scope = rememberCoroutineScope()
            var problem by remember { mutableStateOf<String?>(null) }
            Column {
                problem?.let { Muted(it, Modifier.testTag("complete-error")) }
                NowResultsContent(results, done, now, zone, reload, onCheck = { task ->
                    scope.launch { problem = completeProblem(completeTask(task, done, 0L, complete)); reload++ }
                }, onCard = { opened += it }, onTask = {})
            }
        } } }
    }

    @Test fun checkedTaskStaysHiddenUntilServerDropsIt() {
        server.serve("results.cards_today", "results.cards_around", "results.tasks")
        server.serveDerived("results.tasks") { JsonObject(it.jsonObject + ("items" to JsonArray(emptyList()))) }
        val done = DoneStore(File(tmp.root, "done.json"))
        harness(done, { _, _ -> CompleteResult.Offline })
        await("task-card_request")
        compose.onNodeWithTag("task-check-card_request").performClick()
        gone("task-card_request")
        assertEquals(setOf("card_request"), done.ids.value)
        // The server is out of reach: the saved copy still lists the task, which must stay hidden.
        server.offline = true
        compose.runOnIdle { reload++ }
        compose.waitForIdle()
        assertTrue(!has("task-card_request"))
        assertEquals(setOf("card_request"), done.ids.value)
        // Connected again: the server's list no longer has it, so the local mark is dropped.
        server.offline = false
        compose.runOnIdle { reload++ }
        compose.waitUntil(10_000) { done.ids.value.isEmpty() }
        assertTrue(!has("task-card_request"))
    }

    @Test fun rejectedCompleteReappears() {
        server.serve("results.cards_today", "results.cards_around", "results.tasks")
        val done = DoneStore(File(tmp.root, "done.json"))
        harness(done, { _, _ -> CompleteResult.Rejected("revision_conflict") })
        await("task-card_request")
        compose.onNodeWithTag("task-check-card_request").performClick()
        await("complete-error")
        compose.onNodeWithTag("complete-error").assertTextContains("revision_conflict", substring = true)
        await("task-card_request")
        assertTrue(done.ids.value.isEmpty())
    }

    @Test fun alreadyResolvedStaysHidden() {
        server.serve("results.cards_today", "results.cards_around", "results.tasks")
        val done = DoneStore(File(tmp.root, "done.json"))
        harness(done, { _, _ -> CompleteResult.AlreadyResolved })
        await("task-card_request")
        compose.onNodeWithTag("task-check-card_request").performClick()
        gone("task-card_request")
        assertTrue(!has("complete-error"))
    }

    @Test fun sectionsShowTheServerWordsAndCounts() {
        server.serve("results.cards_today", "results.cards_around", "results.tasks")
        harness(DoneStore(File(tmp.root, "done.json")), { _, _ -> applied() })
        await("now-tasks")
        compose.onNodeWithText("내 남은 일 1").assertIsDisplayed()
        compose.onNodeWithText("체크하면 완료로 기록").assertIsDisplayed()
        compose.onNode(hasText("D-2") and hasAnyAncestor(hasTestTag("now-tasks"))).assertIsDisplayed()
        compose.onNode(hasText("네트워크") and hasAnyAncestor(hasTestTag("now-tasks"))).assertIsDisplayed()
        compose.onNodeWithText("오늘 들어온 일").assertIsDisplayed()
        compose.onNodeWithText("앞뒤 7일").assertIsDisplayed()
        compose.onNodeWithText("11/5 – 11/19").assertIsDisplayed()
        compose.onNodeWithText("내일").assertIsDisplayed()
    }

    @Test fun emptyResultsShowTheEmptyText() {
        server.serveDerived("results.cards_today") { JsonObject(it.jsonObject + ("items" to JsonArray(emptyList()))) }
        server.serveDerived("results.cards_around") { JsonObject(it.jsonObject + ("items" to JsonArray(emptyList()))) }
        server.serveDerived("results.tasks") { JsonObject(it.jsonObject + ("items" to JsonArray(emptyList()))) }
        harness(DoneStore(File(tmp.root, "done.json")), { _, _ -> applied() })
        await("now-results-empty")
        compose.onNodeWithText("지금 처리할 일이 없어요").assertIsDisplayed()
        assertTrue(!has("now-tasks") && !has("now-today") && !has("now-around"))
    }

    @Test fun unreachableServerWithoutACopyIsNotAnEmptyDay() {
        server.offline = true
        harness(DoneStore(File(tmp.root, "done.json")), { _, _ -> applied() })
        await("now-results-unavailable")
        assertTrue(!has("now-results-empty"))
    }

    @Test fun reflectingBadge() {
        server.serve("results.cards_today", "results.cards_around", "results.tasks")
        harness(DoneStore(File(tmp.root, "done.json")), { _, _ -> applied() })
        await("now-today")
        compose.onNodeWithTag("now-card-966cc4e521f2493d986c4e49").assertTextContains("반영중", substring = true)
    }

    @Test fun sectionOrder() {
        ApprovalFixtures.prepare()
        server.serve("results.cards_today", "results.cards_around", "results.tasks")
        val results = results()
        val done = DoneStore(File(tmp.root, "done.json"))
        compose.setContent { MaterialTheme { WeaverTheme {
            NowScreen(null, StatusCounts(0, 0, 0), now, connected = true, onOpenSettings = {}, zone = zone,
                results = { NowResultsContent(results, done, now, zone, 0, {}, {}, {}) })
        } } }
        compose.waitUntil(10_000) { has("approval-section") && has("now-tasks") && has("now-today") && has("now-around") }
        val tops = listOf("approval-section", "now-tasks", "now-today", "now-around")
            .map { compose.onNodeWithTag(it).fetchSemanticsNode().boundsInRoot.top }
        assertEquals(tops.sorted(), tops)
        assertEquals(4, tops.toSet().size)
    }

    @Test fun cardLinksOpenTheirDocumentBulletOrDigestLine() {
        val card = ResultCard("c1", "2026-11-12", "회사/infra/network", "회사", "견적 회신", emptyList(), 0, 1, emptyList(), emptyList(),
            null, null, null, listOf("t1"), null, false)
        fun link(kind: LinkKind, bullet: String?, block: String?) =
            card.copy(link = io.github.junhcha.weaver.schema.LinkTarget(kind, "d1", bullet, block))
        assertEquals(LinkTarget.Document("d1", "b1"), link(LinkKind.DOCUMENT, "b1", null).openTarget())
        assertEquals(LinkTarget.Document("d1", "blk"), link(LinkKind.DIGEST, null, "blk").openTarget())
        assertEquals(LinkTarget.Document("d1", null), link(LinkKind.DIGEST, null, null).openTarget())
        assertEquals(null, card.openTarget())
    }

    // ── PhoneShell: where a card leads and what an old server keeps ──

    private fun model(results: ResultClient?) = object : PhoneReadModel by SyntheticPhoneReadModel(Clock.fixed(now, ZoneId.of("UTC"))) {
        override val results: ResultClient? = results
        override val doneStore: DoneStore? = DoneStore(File(tmp.root, "shell-done.json"))
    }

    private fun showShell(model: PhoneReadModel) = compose.setContent {
        MaterialTheme { WeaverTheme { PhoneShell(model, StatusCounts(0, 0, 0), { now }, {}, {}) } }
    }

    private fun serveDigestCards() {
        server.serveDerived("results.cards_today") { body ->
            val items = body.jsonObject.getValue("items").jsonArray.map { item ->
                val link = item.jsonObject["link"]
                if (link is JsonObject && link["kind"] == JsonPrimitive("digest"))
                    JsonObject(item.jsonObject + ("link" to JsonObject(link + mapOf(
                        "document_id" to JsonPrimitive(ResultFixtures.documentId), "block_id" to JsonPrimitive("w-2e5461320b")))))
                else item
            }
            JsonObject(body.jsonObject + ("items" to JsonArray(items)))
        }
        server.serve("results.cards_around", "results.tasks", "results.document", "results.evidence")
    }

    @Test fun cardOpensDigestLine() {
        serveDigestCards()
        showShell(model(results()))
        await("now-card-b2ba98e05427f94c5094787e")
        compose.onNodeWithTag("now-card-b2ba98e05427f94c5094787e").performClick()
        await("document-screen")
        compose.onNodeWithTag("tab-AREAS").assertIsDisplayed()
        val focused = compose.onNodeWithTag("doc-bullet-w-2e5461320b").fetchSemanticsNode().config
        assertEquals("강조됨", focused[SemanticsProperties.StateDescription])
    }

    @Test fun serverWithoutResultsKeepsLegacyNow() {
        server.capabilities(features = mapOf("results" to false))
        server.serve("results.cards_today", "results.cards_around", "results.tasks")
        showShell(model(results()))
        compose.waitUntil(10_000) { compose.onAllNodes(androidx.compose.ui.test.hasText("기한 지남")).fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("기한 지남").assertIsDisplayed()
        assertTrue(!has("now-tasks") && !has("now-today"))
    }

    @Test fun documentBackButtonReturnsToNowAndLeavesNoStaleReturn() {
        serveDigestCards()
        server.serve("results.tree")
        showShell(model(results()))
        await("now-card-b2ba98e05427f94c5094787e")
        compose.onNodeWithTag("now-card-b2ba98e05427f94c5094787e").performClick()
        await("document-screen")
        compose.onNodeWithTag("document-back").performClick()
        await("now-screen")
        compose.onNodeWithTag("tab-NOW").assertIsSelected()
        // The flag is gone: a document opened later from 영역 closes back to 영역.
        compose.onNodeWithTag("tab-AREAS").performClick()
        await("area-node-회사/infra/network")
        compose.onNodeWithTag("area-node-회사/infra/network").performClick()
        await("document-screen")
        compose.onNodeWithTag("document-back").performClick()
        compose.waitForIdle()
        compose.onNodeWithTag("tab-AREAS").assertIsSelected()
    }

    @Test fun undoShowsTheTaskAgainOnSuccessAndKeepsItHiddenOnFailure() {
        val done = DoneStore(File(tmp.root, "done.json"))
        val task = TaskItem("t1", 1, "회의실 예약", "b1", null, null, null, null, null, null)
        val operation = (applied() as CompleteResult.Done).operation
        done.hide("t1", 1)
        assertEquals("되돌리지 못했어요", kotlinx.coroutines.runBlocking { undoCompletion(task, operation, done) { error("offline") } })
        assertEquals(setOf("t1"), done.ids.value)
        assertEquals(null, kotlinx.coroutines.runBlocking { undoCompletion(task, operation, done) { assertEquals(operation, it) } })
        assertTrue(done.ids.value.isEmpty())
    }

    @Test fun completeToastOffersUndo() {
        var undone = 0
        compose.setContent { MaterialTheme { WeaverTheme { CompleteToast("회의실 예약") { undone++ } } } }
        compose.onNodeWithTag("complete-toast").assertIsDisplayed()
        compose.onNodeWithText("완료함 · 회의실 예약").assertIsDisplayed()
        compose.onNodeWithTag("complete-undo").assertTextContains("되돌리기").performClick()
        assertEquals(1, undone)
    }
}
