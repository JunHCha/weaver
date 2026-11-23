package io.github.junhcha.weaver.ui

import androidx.activity.ComponentActivity
import androidx.compose.material3.MaterialTheme
import androidx.compose.ui.test.junit4.StateRestorationTester
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import io.github.junhcha.weaver.phone.PhoneReadModel
import io.github.junhcha.weaver.phone.ResultClient
import io.github.junhcha.weaver.phone.StatusCounts
import io.github.junhcha.weaver.phone.SyntheticPhoneReadModel
import io.github.junhcha.weaver.transport.FakeMobileServer
import io.github.junhcha.weaver.transport.HttpsMobileApi
import java.time.Clock
import java.time.Instant
import java.time.ZoneId
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** Back, tab re-tap and recreation in the 영역 tab with server results (PhoneShell over a fixture server). */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36], qualifiers = "w411dp-h800dp-xhdpi")
class AreasNavigationTest {
    @get:Rule val compose = createAndroidComposeRule<ComponentActivity>()
    @get:Rule val tmp = TemporaryFolder()
    private val server = FakeMobileServer()
    private val now = Instant.parse("2026-11-12T09:00:00Z")

    @After fun stop() { server.close() }

    private fun shell(): @androidx.compose.runtime.Composable () -> Unit {
        server.serve("results.tree", "results.document", "results.evidence")
        server.serveDerived("results.references", ResultFixtures::withReference)
        val results = ResultClient({ HttpsMobileApi(FakeMobileServer.pairing(server), connectTimeoutMs = 500, readTimeoutMs = 1000) }, tmp.root) { now }
        val model = object : PhoneReadModel by SyntheticPhoneReadModel(Clock.fixed(now, ZoneId.of("UTC"))) {
            override val results: ResultClient = results
        }
        return { MaterialTheme { WeaverTheme { PhoneShell(model, StatusCounts(0, 0, 0), { now }, {}, {}, initialTab = PhoneTab.AREAS) } } }
    }

    private fun show() = compose.setContent(shell())
    private fun has(tag: String) = compose.onAllNodes(hasTestTag(tag)).fetchSemanticsNodes().isNotEmpty()
    private fun await(tag: String) = compose.waitUntil(10_000) { has(tag) }
    private fun gone(tag: String) = compose.waitUntil(10_000) { !has(tag) }
    private fun back() { compose.runOnUiThread { compose.activity.onBackPressedDispatcher.onBackPressed() }; compose.waitForIdle() }
    private fun openNetwork() { await("area-node-회사/infra/network"); compose.onNodeWithTag("area-node-회사/infra/network").performClick(); await("document-screen") }
    private fun openEvidence() { compose.onNodeWithTag("doc-bullet-b1").performClick(); await("evidence-sheet") }

    @Test fun narrowBackGoesEvidenceThenDocumentThenNodeThenRoot() {
        show()
        // The node click selects the path and then its document; the document must survive the path change.
        openNetwork()
        openEvidence()
        back()
        gone("evidence-sheet")
        compose.onNodeWithTag("document-screen").assertIsDisplayed()
        back()
        gone("document-screen")
        await("area-node-detail")
        back()
        gone("area-node-detail")
        await("area-tree")
        compose.onNodeWithTag("area-node-회사").assertIsDisplayed()
    }

    @Test @Config(qualifiers = "w900dp-h600dp")
    fun wideBackClearsTheEvidencePaneFirst() {
        show()
        openNetwork()
        openEvidence()
        back()
        gone("evidence-sheet")
        compose.onNodeWithTag("document-screen").assertIsDisplayed()
        compose.onNodeWithTag("area-tree").assertIsDisplayed()
        back()
        gone("document-screen")
        await("area-node-detail")
        compose.onNodeWithTag("area-tree").assertIsDisplayed()
    }

    @Test @Config(qualifiers = "w900dp-h600dp")
    fun wideChoosingAnotherNodeReplacesTheOpenDocument() {
        show()
        openNetwork()
        compose.onNodeWithTag("area-node-회사/infra/storage").performClick()
        await("area-node-detail")
        gone("document-screen")
        compose.onNodeWithTag("area-node-회사/infra/network").performClick()
        await("document-screen")
    }

    @Test fun reTappingTheTabClearsTheSelection() {
        show()
        openNetwork()
        openEvidence()
        compose.onNodeWithTag("tab-AREAS").performClick()
        gone("document-screen")
        gone("evidence-sheet")
        await("area-tree")
    }

    @Test fun selectionSurvivesRecreation() {
        val restoration = StateRestorationTester(compose)
        restoration.setContent(shell())
        openNetwork()
        restoration.emulateSavedInstanceStateRestore()
        await("document-screen")
        compose.onNodeWithTag("document-screen").assertIsDisplayed()
        assertEquals(false, has("area-tree-list") && !has("document-screen"))
    }
}
