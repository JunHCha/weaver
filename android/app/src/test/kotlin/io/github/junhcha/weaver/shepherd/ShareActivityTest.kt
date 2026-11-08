package io.github.junhcha.weaver.shepherd

import android.content.ContentProvider
import android.content.ContentValues
import android.database.MatrixCursor
import android.net.Uri
import android.provider.OpenableColumns
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
    private fun launchFile(): ActivityScenario<ShareActivity> {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val uri = Uri.parse("content://transcript/opaque-id")
        org.robolectric.shadows.ShadowContentResolver.registerProviderInternal("transcript", object : ContentProvider() {
            override fun onCreate() = true
            override fun getType(uri: Uri) = "text/plain"
            override fun query(uri: Uri, projection: Array<out String>?, selection: String?, args: Array<out String>?, sort: String?) =
                MatrixCursor(arrayOf(OpenableColumns.DISPLAY_NAME)).apply {
                    addRow(arrayOf("통화 가짜 상대_261108_120030_original.txt"))
                }
            override fun insert(uri: Uri, values: ContentValues?): Uri? = error("read only")
            override fun delete(uri: Uri, selection: String?, args: Array<out String>?) = error("read only")
            override fun update(uri: Uri, values: ContentValues?, selection: String?, args: Array<out String>?) = error("read only")
        })
        org.robolectric.Shadows.shadowOf(context.contentResolver).registerInputStream(uri,
            java.io.ByteArrayInputStream(byteArrayOf(0xfe.toByte(), 0xff.toByte()) + "가짜 대화 😀\n".toByteArray(Charsets.UTF_16BE)))
        return ActivityScenario.launch(Intent(context, ShareActivity::class.java)
            .setAction(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_STREAM, uri))
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
    @Test fun providerDisplayNameDefaultsToMatchingCallAndPreservesUserChoiceOnRecreation() {
        pending = listOf(PendingCall("latest", "2026-11-08T13:00:00+09:00", "가짜 상대"),
            PendingCall("match", "2026-11-08T12:00:28+09:00", "가짜 상대"))
        launchFile().use { scenario ->
            ready()
            compose.onNodeWithTag("channel-CALL").assertIsSelected()
            compose.onNode(hasText("● 2026-11-08T12:00:28", substring = true)).assertExists()
            compose.onNode(hasText("○ 2026-11-08T13:00:00", substring = true)).performScrollTo().performClick()
            compose.onNode(hasText("● 2026-11-08T13:00:00", substring = true)).assertExists()
            compose.onNodeWithTag("channel-SHARE_TEXT").performScrollTo().performClick()
            scenario.recreate()
            ready()
            compose.onNodeWithTag("channel-SHARE_TEXT").assertIsSelected()
            compose.onNodeWithTag("channel-CALL").performClick()
            compose.onNode(hasText("● 2026-11-08T13:00:00", substring = true)).assertExists()
        }
    }
    @Test fun transcriptWithoutPendingCallsKeepsCallDisabledAndExplainsWhy() {
        launchFile().use {
            ready()
            compose.onNodeWithTag("channel-CALL").assertIsNotEnabled()
            compose.onNode(hasText("미처리 통화가 없어", substring = true)).assertExists()
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
