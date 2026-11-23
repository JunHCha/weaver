package io.github.junhcha.weaver.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.junit4.v2.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.click
import androidx.compose.ui.test.percentOffset
import io.github.junhcha.weaver.phone.PhoneReadModel
import io.github.junhcha.weaver.phone.ResultClient
import io.github.junhcha.weaver.phone.StatusCounts
import io.github.junhcha.weaver.phone.SyntheticPhoneReadModel
import io.github.junhcha.weaver.phone.LinkTarget
import io.github.junhcha.weaver.transport.FakeMobileServer
import io.github.junhcha.weaver.transport.HttpsMobileApi
import java.time.Clock
import java.time.Instant
import java.time.ZoneId
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
@Config(sdk = [36], qualifiers = "w411dp-h1600dp-xhdpi")
class AreaTreeTest {
    @get:Rule val compose = createComposeRule()
    @get:Rule val tmp = TemporaryFolder()
    private val server = FakeMobileServer()
    private val now = Instant.parse("2026-11-12T09:00:00Z")

    @After fun stop() { server.close(); assertEquals(emptyList<String>(), server.mismatches.toList()) }

    private fun results() = ResultClient({ HttpsMobileApi(FakeMobileServer.pairing(server), connectTimeoutMs = 500, readTimeoutMs = 1000) }, tmp.root) { now }

    private fun serveAll() {
        server.serve("results.tree", "results.document")
        server.serveDerived("results.references", ResultFixtures::withReference)
        server.serve("results.evidence")
    }

    private fun x(text: String) = compose.onNodeWithText(text, useUnmergedTree = true).fetchSemanticsNode().boundsInRoot
    private fun awaitTag(tag: String) = compose.waitUntil(10_000) { compose.onAllNodesWithTagCount(tag) > 0 }
    private fun androidx.compose.ui.test.SemanticsNodeInteractionsProvider.onAllNodesWithTagCount(tag: String) =
        onAllNodes(androidx.compose.ui.test.hasTestTag(tag)).fetchSemanticsNodes().size

    @Test fun treeShowsSphereAreaTopic() {
        compose.setContent { MaterialTheme { WeaverTheme { AreaTree(ResultFixtures.tree, {}, {}) } } }
        compose.onNodeWithTag("area-node-회사").assertIsDisplayed()
        compose.onNodeWithTag("area-node-회사/infra/network").assertIsDisplayed()
        val order = areaRows(ResultFixtures.tree).map { it.node.path to it.depth }
        assertEquals(listOf("회사" to 0, "회사/infra" to 1, "회사/infra/network" to 2, "회사/infra/storage" to 2), order)
        val sphere = x("회사 · 남은 일 1"); val topic = x("Infra · 남은 일 1"); val leaf = x("Network · 남은 일 1")
        assertTrue("indent grows with depth", sphere.left < topic.left && topic.left < leaf.left)
        assertTrue("parents come before children", sphere.top < topic.top && topic.top < leaf.top)
        compose.onNodeWithTag("area-doc-회사/infra/network").assertIsDisplayed()
        compose.onNodeWithTag("area-doc-회사/infra/storage").assertDoesNotExist()
    }

    @Test fun orphanNodesStayVisibleAsRoots() {
        val rows = areaRows(ResultFixtures.tree.filter { it.path != "회사/infra" })
        assertEquals(mapOf("회사" to 0, "회사/infra/network" to 0, "회사/infra/storage" to 0), rows.associate { it.node.path to it.depth })
        assertEquals(setOf("회사", "회사/infra/network", "회사/infra/storage"), rows.map { it.node.path }.toSet())
    }

    private data class Selection(val path: String? = null, val doc: String? = null, val bullet: String? = null)

    private fun showAreas() {
        serveAll()
        val results = results()
        compose.setContent {
            MaterialTheme { WeaverTheme {
                var s by androidx.compose.runtime.remember { mutableStateOf(Selection()) }
                AreasTab(results, wide = false, s.path, s.doc, s.bullet, null, 0,
                    onPath = { s = Selection(it) }, onDocument = { id, p -> s = Selection(p ?: s.path, id) },
                    onBullet = { s = s.copy(bullet = it) }, onTask = {}, onLog = {}, onCloseDocument = { s = s.copy(doc = null, bullet = null) })
            } }
        }
        awaitTag("area-node-회사/infra/network")
    }

    @Test fun nodeOpensDocumentAndReferences() {
        showAreas()
        compose.onNodeWithTag("area-node-회사/infra/network").performClick()
        awaitTag("document-screen")
        compose.onNodeWithText("네트워크").assertIsDisplayed()
        compose.waitUntil(10_000) { compose.onAllNodesWithText("장비 비교").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("관련 자료").assertIsDisplayed()
        compose.onNodeWithText("장비 비교").assertIsDisplayed()
    }

    @Test fun nodeWithoutDocumentShowsOnlyItsReferences() {
        showAreas()
        compose.onNodeWithTag("area-node-회사/infra/storage").performClick()
        awaitTag("area-node-detail")
        compose.onNodeWithText("Storage").assertIsDisplayed()
        compose.waitUntil(10_000) { compose.onAllNodesWithText("장비 비교").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("관련 자료").assertIsDisplayed()
    }

    @Test fun firstOpenFailureShowsTheUnavailableTextNotAnEmptyTree() {
        server.failWith("/read/tree", "errors.whois_unavailable")
        val results = results()
        compose.setContent { MaterialTheme { WeaverTheme {
            AreasTab(results, false, null, null, null, null, 0, {}, { _, _ -> }, {}, {}, {}, {})
        } } }
        awaitTag("area-tree-unavailable")
        compose.onNodeWithText("불러오지 못했어요. 연결을 확인해 주세요.").assertIsDisplayed()
        compose.onNodeWithText("아직 없어요").assertDoesNotExist()
    }

    @Test fun wikiLinkOpensDocumentWhenResultsAreEnabled() {
        val snapshot = kotlinx.coroutines.runBlocking { SyntheticPhoneReadModel(Clock.fixed(now, ZoneId.of("UTC"))).snapshot() }
        val page = snapshot.wikiPages.first { it.title == "네트워크" }
        val documents = listOf(io.github.junhcha.weaver.schema.DocumentSummary(
            "d-firewall", io.github.junhcha.weaver.schema.DocumentKind.PROJECT, null, "방화벽 장비 교체", null, "a".repeat(64)))
        var opened: LinkTarget? = null
        compose.setContent { MaterialTheme { WeaverTheme {
            WikiViewer(snapshot, page, {}, { opened = it }, resolve = { io.github.junhcha.weaver.phone.resolveLink(snapshot, it, documents) })
        } } }
        compose.onNode(androidx.compose.ui.test.hasText("진행 프로젝트", substring = true))
            .performTouchInput { click(percentOffset(0.9f, 0.5f)) }
        assertEquals(LinkTarget.Document("d-firewall"), opened)
    }

    private fun showShell(readModel: PhoneReadModel, initialDocument: LinkTarget.Document? = null, initialFeedback: Boolean = false) = compose.setContent {
        MaterialTheme { WeaverTheme {
            PhoneShell(readModel, StatusCounts(0, 0, 0), { now }, {}, {}, initialTab = PhoneTab.AREAS, initialDocument = initialDocument, initialFeedback = initialFeedback)
        } }
    }

    private fun model(results: ResultClient) = object : PhoneReadModel by SyntheticPhoneReadModel(Clock.fixed(now, ZoneId.of("UTC"))) {
        override val results: ResultClient = results
    }

    @Test fun overwriteAlertOpensTheDocumentOnItsFeedbackTab() {
        serveAll()
        server.serveDerived("results.feedback_list") { it }
        showShell(model(results()), LinkTarget.Document(ResultFixtures.documentId, null), initialFeedback = true)
        awaitTag("feedback-pane")
        compose.onNodeWithTag("document-segment-feedback").assertIsSelected()
        compose.onNodeWithTag("review-screen").assertDoesNotExist()
    }

    @Test fun shellUsesTheTreeWhenTheServerHasResults() {
        serveAll()
        showShell(model(results()))
        awaitTag("area-tree")
        compose.onNodeWithTag("subject-list").assertDoesNotExist()
    }

    @Test fun shellKeepsTheTemporaryListsWithoutResults() {
        serveAll()
        server.capabilities(features = mapOf("results" to false))
        showShell(model(results()))
        awaitTag("subject-list")
        compose.onNodeWithTag("area-tree").assertDoesNotExist()
    }

    @Test fun documentLinkOpensTheDocumentInAreas() {
        serveAll()
        showShell(model(results()), LinkTarget.Document(ResultFixtures.documentId, "w-2e5461320b"))
        awaitTag("document-screen")
        compose.onNodeWithText("네트워크").assertIsDisplayed()
        compose.onNodeWithTag("doc-bullet-w-2e5461320b").assertIsDisplayed()
    }
}
