package io.github.junhcha.weaver.shepherd

import android.content.Context
import android.content.Intent
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.v2.createEmptyComposeRule
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import org.junit.*
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class ShareActivityTest {
    @get:Rule val compose = createEmptyComposeRule()
    private val original = PendingCalls.repository
    private var pending = emptyList<PendingCall>()
    @Before fun setup() {
        PendingCalls.repository = { object : PendingCallRepository {
            override fun pending() = pending
            override fun markProcessed(callId: String, captureId: String) = true
        } }
    }
    @After fun restore() { PendingCalls.repository = original }
    private fun launch(): ActivityScenario<ShareActivity> {
        val context = ApplicationProvider.getApplicationContext<Context>()
        return ActivityScenario.launch(Intent(context, ShareActivity::class.java)
            .setAction(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, "화면 시험"))
    }
    private fun ready() {
        compose.waitUntil(10_000) {
            compose.onAllNodes(hasText("bytes 준비됨", substring = true)).fetchSemanticsNodes().isNotEmpty()
        }
    }
    @Test fun defaultsToOrdinaryTextAndDisablesCallWithoutPendingCalls() {
        launch().use {
            ready()
            compose.onNodeWithTag("channel-SHARE_TEXT").assertIsSelected()
            compose.onNodeWithTag("channel-CALL").assertIsNotEnabled()
        }
    }
    @Test fun restoresSelectionAndShowsUnknownTimeAndDisplayName() {
        pending = listOf(PendingCall("one", null, null, "공유할 통화 파일"))
        launch().use { scenario ->
            ready()
            compose.onNodeWithTag("channel-CALL").performClick()
            compose.onNode(hasText("시각 미상 · 공유할 통화 파일", substring = true)).assertExists()
            val context = ApplicationProvider.getApplicationContext<Context>()
            val snapshots = java.io.File(context.filesDir, "shepherd-shares").listFiles()!!.map { it.name }.toSet()
            scenario.recreate()
            ready()
            Assert.assertEquals(snapshots, java.io.File(context.filesDir, "shepherd-shares").listFiles()!!.map { it.name }.toSet())
            compose.onNodeWithTag("channel-CALL").assertIsSelected()
            compose.onNode(hasText("● 시각 미상", substring = true)).assertExists()
        }
    }
}
