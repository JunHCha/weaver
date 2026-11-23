package io.github.junhcha.weaver.ui

import android.content.Context
import android.content.Intent
import android.content.pm.ProviderInfo
import android.provider.DocumentsContract
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.test.core.app.ApplicationProvider
import androidx.work.Configuration
import androidx.work.ListenableWorker
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerFactory
import androidx.work.WorkerParameters
import androidx.work.testing.SynchronousExecutor
import androidx.work.testing.WorkManagerTestInitHelper
import io.github.junhcha.weaver.MainActivity
import io.github.junhcha.weaver.courier.CourierClient
import io.github.junhcha.weaver.courier.SafCourierFolder
import io.github.junhcha.weaver.courier.TestDocumentsProvider
import io.github.junhcha.weaver.inbox.InboxDatabase
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.review.ReviewRuntime
import io.github.junhcha.weaver.review.reviewTitle
import io.github.junhcha.weaver.schema.ReviewPackage
import io.github.junhcha.weaver.schema.WeaverCodec
import io.github.junhcha.weaver.storage.AppStorage
import java.io.File
import org.junit.After
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowContentResolver

/** Three-tab home with synthetic phone data and one synthetic review; no map, network or user vault. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class HomeNavigationTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()

    private fun prepare(): ReviewPackage {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val factory = object : WorkerFactory() {
            override fun createWorker(appContext: Context, workerClassName: String, workerParameters: WorkerParameters): ListenableWorker =
                object : Worker(appContext, workerParameters) { override fun doWork(): Result = Result.success() }
        }
        WorkManagerTestInitHelper.initializeTestWorkManager(context, Configuration.Builder()
            .setExecutor(SynchronousExecutor()).setWorkerFactory(factory).build())
        File(AppStorage.exchangeDir(context), "reviews").deleteRecursively()
        File(context.filesDir, "inbox.sqlite3").delete()
        val provider = TestDocumentsProvider()
        provider.attachInfo(context, ProviderInfo().apply {
            authority = "com.android.externalstorage.documents"; exported = true; grantUriPermissions = true
            readPermission = "android.permission.MANAGE_DOCUMENTS"
            writePermission = "android.permission.MANAGE_DOCUMENTS"
        })
        ShadowContentResolver.registerProviderInternal("com.android.externalstorage.documents", provider)
        val tree = DocumentsContract.buildTreeDocumentUri("com.android.externalstorage.documents", TestDocumentsProvider.ROOT)
        context.contentResolver.takePersistableUriPermission(tree,
            Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
        context.getSharedPreferences("courier", Context.MODE_PRIVATE).edit()
            .putString("tree", tree.toString()).putBoolean("stopped", false).commit()
        val p = CourierClient.decoded<ReviewPackage>(File(System.getProperty("weaver.schemaDir"), "examples/ReviewPackage.json").readText())
            .copy(questions = emptyList())
        SafCourierFolder(context.contentResolver, tree).publish("proposals", "${p.packageId}.r${p.revision}.json") {
            it.write(WeaverCodec.encode(p).toByteArray())
        }
        InboxDatabase(context).use { it.reconcile(listOf(p), emptyList(), emptySet(), 1L, tree.toString(), false) }
        InboxRuntime.refresh(context)
        return p
    }

    @After fun stopWork() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        context.getSharedPreferences("courier", Context.MODE_PRIVATE).edit().clear().commit()
        runCatching {
            WorkManager.getInstance(context).cancelAllWork().result.get()
            WorkManagerTestInitHelper.closeWorkDatabase()
        }
    }

    private fun awaitRow(p: ReviewPackage) =
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("approval-row-${p.packageId}").fetchSemanticsNodes().isNotEmpty() }

    private fun back() {
        compose.activityRule.scenario.onActivity { it.onBackPressedDispatcher.onBackPressed() }
        compose.waitForIdle()
    }

    private fun tapInbox() {
        compose.runOnUiThread {
            MainActivity::class.java.getDeclaredMethod("onNewIntent", Intent::class.java)
                .apply { isAccessible = true }.invoke(compose.activity, Intent(InboxRuntime.OPEN_INBOX))
        }
    }

    @Test fun threeTabsOnly() {
        for (tab in listOf("NOW", "AREAS", "LOG")) compose.onNodeWithTag("tab-$tab").assertExists()
        compose.onNodeWithTag("tab-PEOPLE").assertDoesNotExist()
        compose.onNodeWithTag("tab-WIKI").assertDoesNotExist()
        compose.onNodeWithTag("tab-NOW").assertTextEquals("지금")
        compose.onNodeWithTag("tab-AREAS").assertTextEquals("영역")
        compose.onNodeWithTag("tab-LOG").assertTextEquals("로그")
    }

    @Test fun inboxNotificationOpensNowApprovals() {
        val p = prepare()
        compose.onNodeWithTag("tab-LOG").performClick()
        val original = compose.activity.intent
        try {
            tapInbox()
            compose.waitUntil(10_000) { compose.onAllNodesWithTag("approval-section").fetchSemanticsNodes().isNotEmpty() }
            compose.onNodeWithTag("now-screen").assertIsDisplayed()
            compose.onNodeWithTag("tab-NOW").assertIsSelected()
            compose.onNodeWithTag("approval-row-${p.packageId}").assertExists()
            compose.onNodeWithText("내 데이터 흐름").assertDoesNotExist()
            compose.onNodeWithContentDescription("You 노드").assertDoesNotExist()
            // One-shot: the activity dropped its flag and the intent action, so recreation cannot replay it.
            compose.runOnIdle { assertNull(compose.activity.intent.action) }
            val flag = MainActivity::class.java.getDeclaredField("showInbox")
                .apply { isAccessible = true }.get(compose.activity) as androidx.compose.runtime.State<*>
            assertFalse(flag.value as Boolean)
        } finally {
            compose.runOnUiThread { compose.activity.intent = original }
        }
    }

    @Test fun inboxNotificationLeavesSettingsForNowApprovals() {
        val p = prepare()
        compose.onNodeWithText("수집·설정").performClick()
        compose.onNodeWithText("‹ 지금").assertExists()
        val original = compose.activity.intent
        try {
            tapInbox()
            compose.waitUntil(10_000) { compose.onAllNodesWithTag("approval-row-${p.packageId}").fetchSemanticsNodes().isNotEmpty() }
            compose.onNodeWithTag("now-screen").assertIsDisplayed()
        } finally {
            compose.runOnUiThread { compose.activity.intent = original }
        }
    }

    @Test fun reviewRoundTripKeepsLogTabState() {
        val p = prepare()
        // 로그 > 사람 > person is left open before 지금 opens the review; the shell's state must survive the review screen.
        compose.onNodeWithTag("tab-LOG").performClick()
        compose.onNodeWithTag("log-filter-PEOPLE").performClick()
        compose.onNodeWithText("이서준").performClick()
        compose.onNodeWithTag("timeline-screen").assertIsDisplayed()
        compose.onNodeWithTag("tab-NOW").performClick()
        awaitRow(p)
        compose.onNodeWithTag("approval-row-${p.packageId}").performClick()
        compose.waitUntil(10_000) { compose.onAllNodesWithText(p.reviewTitle()).fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithTag("review-screen").assertIsDisplayed()
        back()
        compose.onNodeWithTag("now-screen").assertIsDisplayed()
        compose.onNodeWithTag("tab-NOW").assertIsSelected()
        compose.onNodeWithTag("tab-LOG").performClick()
        compose.onNodeWithTag("timeline-screen").assertIsDisplayed()
    }

    @Test fun reviewScreenAndShellStateSurviveRecreation() {
        val p = prepare()
        compose.onNodeWithTag("tab-LOG").performClick()
        compose.onNodeWithTag("log-filter-PEOPLE").performClick()
        compose.onNodeWithTag("tab-NOW").performClick()
        awaitRow(p)
        compose.onNodeWithTag("approval-row-${p.packageId}").performClick()
        compose.onNodeWithTag("review-screen").assertIsDisplayed()
        compose.activityRule.scenario.recreate()
        compose.onNodeWithTag("review-screen").assertIsDisplayed()
        back()
        compose.onNodeWithTag("tab-NOW").assertIsSelected()
        compose.onNodeWithTag("tab-LOG").performClick()
        compose.onNodeWithTag("log-filter-PEOPLE").assertIsOn()
    }

    @Test fun approveReturnsToNowWithoutRow() {
        val p = prepare()
        val context = ApplicationProvider.getApplicationContext<Context>()
        awaitRow(p)
        compose.onNodeWithTag("approval-row-${p.packageId}").performClick()
        compose.waitUntil(10_000) { compose.onAllNodesWithText("최종 검토").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("최종 검토").performScrollTo().performClick()
        compose.onNodeWithText("승인").performScrollTo().performClick()
        compose.waitUntil(10_000) { ReviewRuntime.records(context).isNotEmpty() }
        // The refresh wait ends once the item leaves the list or after its 3 second cap.
        compose.mainClock.advanceTimeBy(3_500)
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("review-screen").fetchSemanticsNodes().isEmpty() }
        compose.onNodeWithTag("now-screen").assertIsDisplayed()
        compose.onNodeWithTag("tab-NOW").assertIsSelected()
        compose.onNodeWithTag("approval-row-${p.packageId}").assertDoesNotExist()
    }

    @Test fun wikiLinkOpensDocumentInAreas() {
        compose.onNodeWithTag("tab-AREAS").performClick()
        // "네트워크" is both an area and a document: the document row is listed second.
        compose.onAllNodesWithText("네트워크")[1].performClick()
        compose.onNodeWithTag("wiki-viewer").assertIsDisplayed()
        compose.onNodeWithText("진행 중").assertDoesNotExist()
        compose.onNode(hasText("진행 프로젝트", substring = true)).performTouchInput { click(percentOffset(0.9f, 0.5f)) }
        compose.onNodeWithTag("wiki-viewer").assertIsDisplayed()
        compose.onNodeWithTag("tab-AREAS").assertIsSelected()
        compose.onNodeWithText("진행 중").assertIsDisplayed()
    }

    @Test fun personLinkOpensTimelineInLog() {
        compose.onNodeWithTag("tab-AREAS").performClick()
        compose.onNodeWithText("방화벽 장비 교체").performClick()
        compose.onNodeWithText("나, 이서준").performClick()
        compose.onNodeWithTag("tab-LOG").assertIsSelected()
        compose.onNodeWithTag("timeline-screen").assertIsDisplayed()
    }

    @Test fun logListsAllThreadsAndFiltersPeople() {
        compose.onNodeWithTag("tab-LOG").performClick()
        compose.onNodeWithTag("log-filter-ALL").assertIsOn()
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("log-threads").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithTag("log-filter-PEOPLE").performClick()
        compose.onNodeWithTag("subject-list").assertIsDisplayed()
        compose.onNodeWithText("이서준").assertIsDisplayed()
    }

    @Test fun statusStripIsNotAButton() {
        compose.onNodeWithTag("status-strip").assertHasNoClickAction()
        val config = compose.onNodeWithTag("status-strip").fetchSemanticsNode().config
        assertFalse(config.contains(SemanticsActions.OnClick))
        assertFalse(config.contains(SemanticsProperties.Role))
    }
}
