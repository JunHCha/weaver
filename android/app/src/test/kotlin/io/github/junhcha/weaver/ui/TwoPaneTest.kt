package io.github.junhcha.weaver.ui

import android.content.Context
import android.content.Intent
import android.content.pm.ProviderInfo
import android.provider.DocumentsContract
import androidx.compose.material3.MaterialTheme
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
import androidx.compose.ui.unit.dp
import org.robolectric.RuntimeEnvironment
import java.io.File
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowContentResolver

/** Fold layouts: two panes when wide, the detail full screen when narrow; synthetic data only. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class TwoPaneTest {
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
            .copy(sourceText = "통화 원문 전체")
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

    @Test fun isWideRule() {
        assertTrue(isWide(840.dp, 600.dp))
        assertFalse(isWide(839.dp, 600.dp))
        assertFalse(isWide(900.dp, 1000.dp))
    }

    @Test @Config(qualifiers = "w900dp-h600dp")
    fun wideShowsListAndDetail() {
        val p = prepare()
        awaitRow(p)
        compose.onNodeWithTag("approval-row-${p.packageId}").performClick()
        compose.waitUntil(10_000) { compose.onAllNodesWithText(p.reviewTitle()).fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithTag("approval-section").assertIsDisplayed()
        compose.onNodeWithTag("review-screen").assertIsDisplayed()
        back()
        compose.onNodeWithTag("review-screen").assertDoesNotExist()
        awaitRow(p)
        compose.onNodeWithTag("approval-section").assertIsDisplayed()
    }

    @Test @Config(qualifiers = "w400dp-h800dp")
    fun narrowShowsDetailFullScreen() {
        val p = prepare()
        awaitRow(p)
        compose.onNodeWithTag("approval-row-${p.packageId}").performClick()
        compose.onNodeWithTag("review-screen").assertIsDisplayed()
        compose.onNodeWithTag("approval-section").assertDoesNotExist()
        back()
        compose.onNodeWithTag("review-screen").assertDoesNotExist()
        awaitRow(p)
        compose.onNodeWithTag("approval-section").assertIsDisplayed()
    }

    @Test @Config(qualifiers = "w900dp-h600dp")
    fun wideReviewShowsSourceSideBySide() {
        val p = prepare()
        awaitRow(p)
        compose.onNodeWithTag("approval-row-${p.packageId}").performClick()
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("review-source").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithTag("review-source").assertIsDisplayed()
        compose.onNode(hasText("통화 원문 전체") and hasAnyAncestor(hasTestTag("review-source"))).assertIsDisplayed()
        compose.onNodeWithText("전체 원문 보기").assertDoesNotExist()
    }

    @Test @Config(qualifiers = "w900dp-h600dp")
    fun foldChangeKeepsReviewInput() {
        val p = prepare()
        awaitRow(p)
        compose.onNodeWithTag("approval-row-${p.packageId}").performClick()
        compose.waitUntil(10_000) { compose.onAllNodesWithText("직접 답변 1").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("직접 답변 1").performTextInput("접힘 전 답변")
        RuntimeEnvironment.setQualifiers("w400dp-h800dp")
        compose.activityRule.scenario.recreate()
        compose.waitUntil(10_000) { compose.onAllNodesWithText("접힘 전 답변").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithTag("review-screen").assertIsDisplayed()
        compose.onNodeWithText("접힘 전 답변").assertExists()
        // And back to the wide layout the same package and answer are still there.
        RuntimeEnvironment.setQualifiers("w900dp-h600dp")
        compose.activityRule.scenario.recreate()
        compose.waitUntil(10_000) { compose.onAllNodesWithText("접힘 전 답변").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithTag("review-source").assertIsDisplayed()
        assertEquals(1, compose.onAllNodesWithTag("review-source").fetchSemanticsNodes().size)
    }
}

/** The wide 지금 and 영역 tabs over the synthetic read model. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36], qualifiers = "w900dp-h600dp")
class TwoPaneShellTest {
    @get:Rule val compose = androidx.compose.ui.test.junit4.v2.createComposeRule()

    private val now = java.time.OffsetDateTime.parse("2026-11-18T09:00:00+09:00").toInstant()
    private val model = io.github.junhcha.weaver.phone.SyntheticPhoneReadModel(
        java.time.Clock.fixed(now, java.time.ZoneId.of("UTC")))

    private fun show() = compose.setContent {
        MaterialTheme { WeaverTheme {
            PhoneShell(model, io.github.junhcha.weaver.phone.StatusCounts(0, 0, 0), { now }, {}, {})
        } }
    }

    @Test fun wideThreadShowsInlineDetail() {
        show()
        compose.onNodeWithText("협력사 A 견적서 회신").performClick()
        val inline = hasTestTag("now-thread-detail") and hasAnyAncestor(hasTestTag("two-pane-detail"))
        compose.onNode(inline).assertIsDisplayed()
        // A bottom sheet would add a second detail node outside the pane.
        assertEquals(1, compose.onAllNodesWithTag("now-thread-detail").fetchSemanticsNodes().size)
    }

    @Test fun wideAreasShowsDocumentBesideList() {
        show()
        compose.onNodeWithTag("tab-AREAS").performClick()
        compose.onNodeWithText("방화벽 장비 교체").performClick()
        compose.onNodeWithTag("wiki-viewer").assertIsDisplayed()
        compose.onNodeWithTag("wiki-list").assertIsDisplayed()
        compose.onNodeWithTag("subject-list").assertIsDisplayed()
    }
}
