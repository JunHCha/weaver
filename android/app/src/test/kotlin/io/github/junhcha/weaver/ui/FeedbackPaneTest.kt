package io.github.junhcha.weaver.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.test.assertHeightIsAtLeast
import androidx.compose.ui.test.assert
import androidx.compose.ui.semantics.getOrNull
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.junit4.v2.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.phone.ResultClient
import io.github.junhcha.weaver.transport.FakeMobileServer
import io.github.junhcha.weaver.transport.HttpsMobileApi
import java.time.Instant
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.Json
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** The 피드백 tab against the recorded feedback fixtures; every request must equal the fixture's request. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36], qualifiers = "w411dp-h800dp-xhdpi")
class FeedbackPaneTest {
    @get:Rule val compose = createComposeRule()
    @get:Rule val tmp = TemporaryFolder()
    private val server = FakeMobileServer()
    private val feedbackId = "fbk_00000000000000000000000000000001"
    private val note = "견적 회신은 김철수가 아니라 홍길동 담당"
    // A chip appends "[chip] " to what was typed; the note is sent trimmed.
    private val sentText = "$note [사실이 틀림]"

    @After fun stop() { server.close(); assertEquals(emptyList<String>(), server.mismatches.toList()) }

    private fun results() = ResultClient({ HttpsMobileApi(FakeMobileServer.pairing(server), connectTimeoutMs = 500, readTimeoutMs = 1000) }, tmp.root) { Instant.parse("2026-11-12T09:00:00Z") }

    private fun items(body: JsonElement, change: (JsonObject) -> JsonObject?): JsonElement =
        JsonObject(body.jsonObject + ("items" to JsonArray(body.jsonObject["items"]!!.jsonArray.mapNotNull { change(it.jsonObject) })))

    private fun applied(body: JsonElement) = items(body) {
        JsonObject(it + mapOf("status" to JsonPrimitive("applied"), "classification" to JsonPrimitive("fact"), "result" to JsonPrimitive("정정 1건 반영 · 09:30")))
    }

    /** The fixture's submit with the chip's text in the note, which is what the pane sends. */
    private fun serveSubmit() = server.serveDerived("results.feedback_submit",
        request = { JsonObject(it.jsonObject + ("text" to JsonPrimitive(sentText))) }) { it }

    private fun show(keys: Iterator<String> = listOf("feedback-0001").iterator(), bullet: String? = "b1", bulletText: String? = null) {
        val client = results()
        compose.setContent { MaterialTheme { WeaverTheme {
            FeedbackPane(ResultFixtures.documentId, bullet, client, bulletText = bulletText, newKey = { keys.next() })
        } } }
    }

    private fun hasEditableText(text: String) = SemanticsMatcher("editable text is '$text'") {
        it.config.getOrNull(androidx.compose.ui.semantics.SemanticsProperties.EditableText)?.text == text
    }

    private fun awaitTag(tag: String) = compose.waitUntil(10_000) { compose.onAllNodes(hasTestTag(tag)).fetchSemanticsNodes().isNotEmpty() }

    private fun type() {
        compose.onNodeWithTag("feedback-input").performTextInput(note)
        compose.onNodeWithTag("feedback-chip-WRONG_FACT").performClick()
    }

    private fun postBodies() = server.bodies.filter { it.first == "POST /mobile/v1/edits/feedback" }.map { Json.parseToJsonElement(it.second).jsonObject }

    @Test fun sendShowsPendingThenResult() {
        server.serveDerived("results.feedback_list") { items(it) { null } }
        server.serveDerived("results.feedback_list") { applied(it) }
        serveSubmit()
        show()
        awaitTag("feedback-empty")
        compose.onNodeWithTag("feedback-send").assertIsNotEnabled()
        type()
        compose.onNodeWithTag("feedback-send").assertIsEnabled().performClick()
        awaitTag("feedback-item-$feedbackId")
        assertEquals(listOf("사실이 틀림"), postBodies().single()["chips"]!!.jsonArray.map { it.jsonPrimitive.content })
        assertEquals("b1", postBodies().single()["bullet_id"]!!.jsonPrimitive.content)
        compose.onNodeWithTag("feedback-status-$feedbackId").assertTextEquals("반영 중 · Mac 처리 대기")
        compose.onNodeWithTag("feedback-input").assert(hasEditableText(""))
        // The list is refreshed from the server: the Mac applied it.
        compose.onNodeWithTag("feedback-refresh").assertHeightIsAtLeast(48.dp).performClick()
        compose.waitUntil(10_000) { runCatching { compose.onNodeWithTag("feedback-status-$feedbackId").assertTextEquals("정정 1건 반영 · 09:30") }.isSuccess }
        compose.onNodeWithTag("feedback-status-$feedbackId").assertTextEquals("정정 1건 반영 · 09:30")
        compose.onNodeWithTag("feedback-undo-$feedbackId").assertIsDisplayed().assertHeightIsAtLeast(48.dp)
    }

    @Test fun undoPostsAndShowsUndone() {
        server.serveDerived("results.feedback_list") { applied(it) }
        server.serve("results.feedback_undo")
        show(listOf("feedback-undo-01").iterator())
        awaitTag("feedback-undo-$feedbackId")
        compose.onNodeWithTag("feedback-undo-$feedbackId").performClick()
        compose.waitUntil(10_000) { compose.onAllNodes(hasTestTag("feedback-undo-$feedbackId")).fetchSemanticsNodes().isEmpty() }
        compose.onNodeWithTag("feedback-status-$feedbackId").assertTextEquals("되돌림")
        val undo = server.bodies.single { it.first == "POST /mobile/v1/edits/feedback/undo" }
        assertEquals("feedback-undo-01", Json.parseToJsonElement(undo.second).jsonObject["client_id"]!!.jsonPrimitive.content)
    }

    @Test fun retrySendsSameClientId() {
        server.serveDerived("results.feedback_list") { items(it) { null } }
        serveSubmit()
        server.dropResponse += "/edits/feedback"
        // The second key must never be used: a retry of the same input keeps the first.
        show(listOf("feedback-0001", "feedback-other").iterator())
        awaitTag("feedback-empty")
        type()
        compose.onNodeWithTag("feedback-send").performClick()
        awaitTag("feedback-error")
        compose.onNodeWithTag("feedback-input").assertTextContains(note, substring = true)
        compose.onNodeWithTag("feedback-send").assertIsEnabled().performClick()
        awaitTag("feedback-item-$feedbackId")
        assertEquals(listOf("feedback-0001", "feedback-0001"), postBodies().map { it["client_id"]!!.jsonPrimitive.content })
    }

    @Test fun quotesTheSentenceAndHidesTheQuoteWithoutOne() {
        server.serveDerived("results.feedback_list") { items(it) { null } }
        show(bulletText = "장비 2종 견적 도착")
        awaitTag("feedback-quote")
        compose.onNodeWithTag("feedback-quote").assertTextContains("장비 2종 견적 도착", substring = true)
    }

    @Test fun failedAndUndoneRowsHaveNoUndo() {
        server.serveDerived("results.feedback_list") { items(it) { o -> JsonObject(o + ("status" to JsonPrimitive("failed"))) } }
        show(bullet = null)
        awaitTag("feedback-status-$feedbackId")
        compose.onNodeWithTag("feedback-status-$feedbackId").assertTextEquals("처리하지 못했어요")
        compose.onNodeWithTag("feedback-undo-$feedbackId").assertDoesNotExist()
        compose.onNodeWithTag("feedback-quote").assertDoesNotExist()
    }

    @Test fun unreadableListSaysSoAndStillAllowsSending() {
        server.offline = true
        show()
        awaitTag("feedback-unavailable")
        compose.onNodeWithTag("feedback-input").assertIsDisplayed()
    }

    @Test fun chipTogglesItsTextAndTargetsAreBigEnough() {
        server.serveDerived("results.feedback_list") { items(it) { null } }
        show(bullet = null)
        awaitTag("feedback-empty")
        compose.onNodeWithTag("feedback-input").performTextInput("메모")
        compose.onNodeWithTag("feedback-chip-ALREADY_DONE").assertHeightIsAtLeast(48.dp).performClick()
        compose.onNodeWithTag("feedback-input").assertTextContains("메모 [이미 끝남] ", substring = true)
        compose.onNodeWithTag("feedback-chip-ALREADY_DONE").performClick()
        compose.onNodeWithTag("feedback-input").assert(hasEditableText("메모"))
        compose.onNodeWithTag("feedback-send").assertHeightIsAtLeast(48.dp)
    }

    @Test fun documentTabAppearsOnlyWithTheServerFeature() {
        server.serve("results.evidence")
        server.capabilities(features = mapOf("feedback" to false))
        val client = results()
        compose.setContent { MaterialTheme { WeaverTheme {
            var bullet by remember { mutableStateOf<String?>(null) }
            DocumentPane(ResultFixtures.document, client, bullet, { bullet = it }, {}, {}, null, false)
        } } }
        compose.onNodeWithTag("document-screen").assertIsDisplayed()
        compose.waitForIdle()
        Thread.sleep(300)
        compose.waitForIdle()
        compose.onNodeWithTag("document-segments").assertDoesNotExist()
        compose.onNodeWithTag("doc-bullet-b1").performClick()
        awaitTag("evidence-sheet")
        compose.onNodeWithTag("evidence-feedback").assertIsNotEnabled()
    }

    @Test fun digestHasNoFeedbackTab() {
        val client = results()
        val digest = ResultFixtures.document.copy(kind = io.github.junhcha.weaver.schema.DocumentKind.DIGEST)
        compose.setContent { MaterialTheme { WeaverTheme { DocumentPane(digest, client, null, {}, {}, {}, null, false) } } }
        compose.onNodeWithTag("document-screen").assertIsDisplayed()
        compose.waitForIdle()
        Thread.sleep(300)
        compose.waitForIdle()
        compose.onNodeWithTag("document-segments").assertDoesNotExist()
    }

    @Test fun evidenceWrongOpensFeedbackWithBullet() {
        server.serve("results.evidence")
        server.serveDerived("results.feedback_list") { items(it) { null } }
        serveSubmit()
        val client = results()
        val keys = listOf("feedback-0001").iterator()
        compose.setContent { MaterialTheme { WeaverTheme {
            androidx.compose.foundation.layout.Box {
                var bullet by remember { mutableStateOf<String?>(null) }
                DocumentPane(ResultFixtures.document, client, bullet, { bullet = it }, {}, {}, null, false, feedbackKey = { keys.next() })
            }
        } } }
        compose.onNodeWithTag("doc-bullet-b1").performClick()
        awaitTag("evidence-sheet")
        compose.waitUntil(10_000) { runCatching { compose.onNodeWithTag("evidence-feedback").assertIsEnabled() }.isSuccess }
        compose.onNodeWithTag("evidence-feedback").performClick()
        awaitTag("feedback-pane")
        compose.onNodeWithTag("document-segment-feedback").assertHeightIsAtLeast(48.dp)
        compose.onNodeWithTag("feedback-quote").assertTextContains("장비 2종 견적 도착", substring = true)
        compose.onNodeWithTag("evidence-sheet").assertDoesNotExist()
        compose.onNodeWithTag("feedback-input").performTextInput(note)
        compose.onNodeWithTag("feedback-chip-WRONG_FACT").performClick()
        compose.onNodeWithTag("feedback-send").performClick()
        awaitTag("feedback-item-$feedbackId")
        assertEquals("b1", postBodies().single()["bullet_id"]!!.jsonPrimitive.content)
    }

    @Test fun quoteIsForgottenAfterSendingAndAGeneralNoteHasNoBullet() {
        server.serveDerived("results.feedback_list") { items(it) { null } }
        serveSubmit()
        server.serveDerived("results.feedback_submit",
            request = { JsonObject(it.jsonObject.filterKeys { k -> k != "bullet_id" } + mapOf("text" to JsonPrimitive(sentText), "client_id" to JsonPrimitive("feedback-0002"))) }) { it }
        val client = results()
        val keys = listOf("feedback-0001", "feedback-0002").iterator()
        compose.setContent { MaterialTheme { WeaverTheme {
            var bullet by remember { mutableStateOf<String?>("b1") }
            FeedbackPane(ResultFixtures.documentId, bullet, client, bulletText = "장비 2종 견적 도착", newKey = { keys.next() }, onBulletCleared = { bullet = null })
        } } }
        awaitTag("feedback-empty")
        type()
        compose.onNodeWithTag("feedback-send").performClick()
        awaitTag("feedback-item-$feedbackId")
        compose.onNodeWithTag("feedback-quote").assertDoesNotExist()
        compose.waitUntil(10_000) { runCatching { compose.onNodeWithTag("feedback-input").assert(hasEditableText("")) }.isSuccess }
        type()
        compose.onNodeWithTag("feedback-send").performClick()
        compose.waitUntil(10_000) { postBodies().size == 2 }
        assertEquals("b1", postBodies()[0]["bullet_id"]!!.jsonPrimitive.content)
        assertTrue("no bullet_id on the general note", "bullet_id" !in postBodies()[1])
    }

    @Test fun quoteCanBeRemovedWithTheCross() {
        server.serveDerived("results.feedback_list") { items(it) { null } }
        val client = results()
        compose.setContent { MaterialTheme { WeaverTheme {
            var bullet by remember { mutableStateOf<String?>("b1") }
            FeedbackPane(ResultFixtures.documentId, bullet, client, bulletText = "장비 2종 견적 도착", onBulletCleared = { bullet = null })
        } } }
        awaitTag("feedback-quote")
        compose.onNodeWithTag("feedback-quote-clear").assertHeightIsAtLeast(48.dp).performClick()
        compose.onNodeWithTag("feedback-quote").assertDoesNotExist()
    }

    @Test fun pendingItemCanBeUndone() {
        server.serve("results.feedback_list")
        server.serve("results.feedback_undo")
        show(listOf("feedback-undo-01").iterator())
        awaitTag("feedback-status-$feedbackId")
        compose.onNodeWithTag("feedback-status-$feedbackId").assertTextEquals("반영 중 · Mac 처리 대기")
        compose.onNodeWithTag("feedback-undo-$feedbackId").performClick()
        compose.waitUntil(10_000) { compose.onAllNodes(hasTestTag("feedback-undo-$feedbackId")).fetchSemanticsNodes().isEmpty() }
        compose.onNodeWithTag("feedback-status-$feedbackId").assertTextEquals("되돌림")
    }

    @Test fun serverCodesReadAsWordsWithAGenericFallback() {
        assertTrue(feedbackProblem("document_not_found").contains("문서"))
        assertEquals(feedbackProblem("some_new_code"), feedbackProblem("another"))
        assertTrue("no raw code shown", !feedbackProblem("some_new_code").contains("some_new_code"))
    }
}
