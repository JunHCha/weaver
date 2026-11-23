package io.github.junhcha.weaver.ui

import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.test.assertHeightIsAtLeast
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.hasAnyAncestor
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.junit4.v2.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import io.github.junhcha.weaver.phone.ResultClient
import io.github.junhcha.weaver.schema.TaskItem
import io.github.junhcha.weaver.transport.FakeMobileServer
import io.github.junhcha.weaver.transport.HttpsMobileApi
import java.time.Instant
import androidx.compose.ui.unit.dp
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36], qualifiers = "w411dp-h800dp-xhdpi")
class DocumentScreenTest {
    @get:Rule val compose = createComposeRule()
    @get:Rule val tmp = TemporaryFolder()
    private val server = FakeMobileServer()

    @After fun stop() { server.close(); assertEquals(emptyList<String>(), server.mismatches.toList()) }

    private fun results() = ResultClient({ HttpsMobileApi(FakeMobileServer.pairing(server), connectTimeoutMs = 500, readTimeoutMs = 1000) }, tmp.root) { Instant.parse("2026-11-12T09:00:00Z") }

    private fun top(text: String) = compose.onNodeWithText(text, useUnmergedTree = true).fetchSemanticsNode().boundsInRoot.top

    @Test fun sectionsInOrderAndCautionHiddenWhenEmpty() {
        compose.setContent { MaterialTheme { WeaverTheme { DocumentScreen(ResultFixtures.document, {}, {}, {}, null) } } }
        compose.onNodeWithText("네트워크").assertIsDisplayed()
        compose.onNodeWithText("2026-11-12 기준 · 반영중").assertIsDisplayed()
        assertTrue(top("현재 상태") < top("남은 일"))
        compose.onNodeWithText("주의할 이력").assertDoesNotExist()
        compose.onNodeWithText("기다리는 중").assertDoesNotExist()
        compose.onNodeWithText("관련 자료").assertDoesNotExist()
    }

    @Test fun onlyTheCheckboxCompletesATaskAndTheRowOpensItsEvidence() {
        val tapped = mutableListOf<TaskItem>()
        val evidence = mutableListOf<String>()
        compose.setContent { MaterialTheme { WeaverTheme { DocumentScreen(ResultFixtures.document, { evidence += it }, { tapped += it }, {}, null) } } }
        compose.onNodeWithTag("doc-bullet-w-2e5461320b").performClick()
        assertTrue("a row tap never completes", tapped.isEmpty())
        assertEquals(listOf("w-2e5461320b"), evidence)
        compose.onNodeWithTag("doc-task-check-w-2e5461320b").assertHeightIsAtLeast(48.dp).performClick()
        assertEquals(1, tapped.size)
        assertEquals("the checkbox does not open evidence", 1, evidence.size)
    }

    @Test fun checkedTaskLineShowsDoneAndIgnoresFurtherTaps() {
        val tapped = mutableListOf<TaskItem>()
        val task = ResultFixtures.document.sections.flatMap { it.bullets }.first { it.task != null }.task!!
        compose.setContent { MaterialTheme { WeaverTheme { DocumentScreen(ResultFixtures.document, {}, { tapped += it }, {}, null, doneIds = setOf(task.threadId)) } } }
        compose.onNodeWithTag("doc-task-check-w-2e5461320b").performClick()
        assertTrue(tapped.isEmpty())
    }

    @Test fun everySectionFollowsTheMockupOrder() {
        compose.setContent { MaterialTheme { WeaverTheme { DocumentScreen(ResultFixtures.full, {}, {}, {}, null) } } }
        val tops = listOf("현재 상태", "남은 일", "기다리는 중", "주의할 이력", "관련 자료", "로그에서 시간순으로 보기 →").map { top(it) }
        assertEquals(tops.sorted(), tops)
        compose.onNodeWithTag("doc-caution-thread").assertIsDisplayed()
    }

    @Test fun logLinkAndTaskCheckboxReportToTheCaller() {
        var log: String? = null
        var task: TaskItem? = null
        compose.setContent { MaterialTheme { WeaverTheme { DocumentScreen(ResultFixtures.document, {}, { task = it }, { log = it }, null) } } }
        compose.onNodeWithTag("doc-task-check-w-2e5461320b").performClick()
        assertEquals("견적 회신", task?.content)
        compose.onNodeWithTag("document-log-link").performClick()
        assertEquals("회사/infra/network", log)
    }

    @Test fun focusedBulletIsScrolledIntoView() {
        compose.setContent { MaterialTheme { WeaverTheme { DocumentScreen(ResultFixtures.long, {}, {}, {}, "b40") } } }
        compose.waitForIdle()
        compose.onNodeWithTag("doc-bullet-b40").assertIsDisplayed()
    }

    @Test fun touchTargetsAreAtLeast48dp() {
        compose.setContent { MaterialTheme { WeaverTheme { DocumentScreen(ResultFixtures.full, {}, {}, {}, null) } } }
        compose.onNodeWithTag("doc-bullet-b1").assertHeightIsAtLeast(48.dp)
        compose.onNodeWithTag("document-log-link").assertHeightIsAtLeast(48.dp)
    }

    private fun showPane(wide: Boolean? = null) {
        server.serve("results.evidence")
        val results = results()
        compose.setContent {
            MaterialTheme { WeaverTheme {
                BoxWithConstraints {
                    var bullet by remember { mutableStateOf<String?>(null) }
                    DocumentPane(ResultFixtures.document, results, bullet, { bullet = it }, {}, {}, null, wide ?: isWide(maxWidth, maxHeight))
                }
            } }
        }
    }

    private fun awaitTag(tag: String) = compose.waitUntil(10_000) { compose.onAllNodes(hasTestTag(tag)).fetchSemanticsNodes().isNotEmpty() }

    @Test fun bulletOpensEvidenceWithWholeSource() {
        showPane()
        compose.onNodeWithTag("evidence-sheet").assertDoesNotExist()
        compose.onNodeWithTag("doc-bullet-b1").performClick()
        awaitTag("evidence-sheet")
        compose.onNodeWithTag("evidence-thread-card_request").assertTextContains("견적 회신", substring = true)
        compose.onNodeWithTag("evidence-source-src_00000000000000000000000001")
            .assertTextContains("견적 회신\n장비 목록 수령\n현장 점검")
        compose.onNodeWithText("이 문장을 만든 Thread").assertIsDisplayed()
        compose.onNodeWithText("원문 · 알 수 없음").assertIsDisplayed()
        // The paired fixture server offers feedback, so the button opens the 피드백 tab (see FeedbackPaneTest).
        compose.waitUntil(10_000) { runCatching { compose.onNodeWithTag("evidence-feedback").assertIsEnabled() }.isSuccess }
    }

    @Test @Config(qualifiers = "w900dp-h600dp")
    fun wideShowsEvidenceBesideDocument() {
        showPane()
        compose.onNodeWithTag("doc-bullet-b1").performClick()
        awaitTag("evidence-sheet")
        compose.onNodeWithTag("document-screen").assertIsDisplayed()
        compose.onNode(hasTestTag("evidence-sheet") and hasAnyAncestor(hasTestTag("document-evidence-pane"))).assertIsDisplayed()
        val doc = compose.onNodeWithTag("document-screen").fetchSemanticsNode().boundsInRoot
        val evidence = compose.onNodeWithTag("evidence-sheet").fetchSemanticsNode().boundsInRoot
        assertTrue("side by side", doc.right <= evidence.left)
    }

    @Test fun unreadableEvidenceSaysSo() {
        showPane()
        server.offline = true
        compose.onNodeWithTag("doc-bullet-b1").performClick()
        awaitTag("evidence-unavailable")
    }
}
