package io.github.junhcha.weaver.ui

import android.content.Context
import android.content.Intent
import android.content.pm.ProviderInfo
import android.provider.DocumentsContract
import androidx.activity.compose.setContent
import androidx.compose.material3.MaterialTheme
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.test.core.app.ApplicationProvider
import androidx.work.Configuration
import androidx.work.ListenableWorker
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
import io.github.junhcha.weaver.review.PendingReject
import io.github.junhcha.weaver.review.ReviewDraft
import io.github.junhcha.weaver.review.ReviewRecord
import io.github.junhcha.weaver.review.ReviewRuntime
import io.github.junhcha.weaver.review.ReviewStatus
import io.github.junhcha.weaver.schema.ReviewPackage
import io.github.junhcha.weaver.schema.Stage
import io.github.junhcha.weaver.schema.Verdict
import io.github.junhcha.weaver.schema.WeaverCodec
import io.github.junhcha.weaver.storage.AppStorage
import java.io.File
import java.time.ZoneId
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowContentResolver

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class ApprovalSectionTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()
    private val seoul = ZoneId.of("Asia/Seoul")

    private fun example() = CourierClient.decoded<ReviewPackage>(
        File(System.getProperty("weaver.schemaDir"), "examples/ReviewPackage.json").readText())
    private fun callPackage() = example().copy(
        sourceType = "call", title = "통화 정리",
        stageTimes = mapOf(Stage.COLLECT to "2026-11-18T03:00:00Z"))
    private fun item(p: ReviewPackage, revised: Boolean = false) =
        InboxDatabase.ReviewItem(p.packageId, p.revision, "요약", "검토", revised, 0)
    private fun record(p: ReviewPackage, status: ReviewStatus) =
        ReviewRecord(ReviewDraft.reject(p), "tree", status)

    @Test fun approvalRowsMetaAndBadges() {
        val p = callPackage()
        val rows = approvalRows(listOf(item(p)), mapOf(p.packageId to p), emptyList(), emptyList(), mapOf(p.packageId to "홍길동"), seoul)
        assertEquals("홍길동 · 통화 · 11/18", rows.single().meta)
        assertNull(rows.single().badge)
        assertEquals("통화 정리", rows.single().title)
        assertEquals("다시 확인", approvalRows(listOf(item(p, revised = true)), mapOf(p.packageId to p), emptyList(), emptyList(), emptyMap(), seoul).single().badge)
        val bad = approvalRows(listOf(item(p)), mapOf(p.packageId to null), emptyList(), emptyList(), emptyMap(), seoul).single()
        assertEquals("문서 확인 필요", bad.badge)
        assertFalse(bad.readable)
        assertEquals("요약", bad.title)
        assertEquals("전송 오류", approvalRows(listOf(item(p)), mapOf(p.packageId to p), listOf(record(p, ReviewStatus.ERROR)), emptyList(), emptyMap(), seoul).single().badge)
        assertTrue(approvalRows(listOf(item(p)), mapOf(p.packageId to p), listOf(record(p, ReviewStatus.SENT)), emptyList(), emptyMap(), seoul).isEmpty())
        val pending = PendingReject(p.packageId, p.revision, 1L)
        assertEquals(pending, approvalRows(listOf(item(p)), mapOf(p.packageId to p), emptyList(), listOf(pending), emptyMap(), seoul).single().pendingReject)
    }

    @Test fun metaDateFollowsGivenZone() {
        val p = callPackage().copy(stageTimes = mapOf(Stage.COLLECT to "2026-11-18T20:00:00Z"))
        fun meta(zone: ZoneId) = approvalRows(listOf(item(p)), mapOf(p.packageId to p), emptyList(), emptyList(), emptyMap(), zone).single().meta
        assertEquals("통화 · 11/19", meta(seoul))
        assertEquals("통화 · 11/18", meta(ZoneId.of("UTC")))
    }

    private fun prepare(): Pair<Context, ReviewPackage> {
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
        val p = callPackage()
        SafCourierFolder(context.contentResolver, tree).publish("proposals", "${p.packageId}.r${p.revision}.json") {
            it.write(WeaverCodec.encode(p).toByteArray())
        }
        InboxDatabase(context).use { it.reconcile(listOf(p), emptyList(), emptySet(), 1L, tree.toString(), false) }
        InboxRuntime.refresh(context)
        return context to p
    }

    private fun show(onReview: (String) -> Unit = {}) {
        compose.runOnUiThread { compose.activity.setContent { MaterialTheme { WeaverTheme { ApprovalSection(onReview) } } } }
    }

    @Test fun sectionHiddenWhenEmpty() {
        prepare()
        show()
        compose.waitForIdle()
        compose.onNodeWithTag("approval-section").assertDoesNotExist()
    }

    private fun awaitRow(p: ReviewPackage) =
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("approval-row-${p.packageId}").fetchSemanticsNodes().isNotEmpty() }

    @Test fun tapOpensReview() {
        val (_, p) = prepare()
        var opened: String? = null
        show { opened = it }
        awaitRow(p)
        compose.onNodeWithTag("approval-row-${p.packageId}").performClick()
        assertEquals(p.packageId, opened)
        compose.onNodeWithText("승인 대기 1").assertExists()
    }

    @Test fun swipeLeftSchedulesRejectAndShowsUndo() {
        val (context, p) = prepare()
        show()
        awaitRow(p)
        compose.onNodeWithTag("approval-row-${p.packageId}").performTouchInput { swipeLeft() }
        compose.waitUntil(10_000) { ReviewRuntime.pendingRejects(context).isNotEmpty() }
        assertEquals(p.packageId, ReviewRuntime.pendingRejects(context).single().packageId)
        compose.waitUntil(10_000) { compose.onAllNodesWithText("되돌리기").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("‘통화 정리’ 거절함 · Fabric·Wiki에 반영 안 함").assertExists()
        compose.onNodeWithTag("approval-row-${p.packageId}").assertDoesNotExist()
        compose.onNodeWithText("되돌리기").performClick()
        compose.waitUntil(10_000) { ReviewRuntime.pendingRejects(context).isEmpty() }
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("approval-row-${p.packageId}").fetchSemanticsNodes().isNotEmpty() }
    }

    @Test fun swipingSecondRowRejectsThatRowOnly() {
        val (context, a) = prepare()
        val b = a.copy(packageId = a.packageId + "-b", title = "두번째 통화")
        val tree = context.getSharedPreferences("courier", Context.MODE_PRIVATE).getString("tree", "")!!
        val folder = SafCourierFolder(context.contentResolver, android.net.Uri.parse(tree))
        folder.publish("proposals", "${b.packageId}.r${b.revision}.json") { it.write(WeaverCodec.encode(b).toByteArray()) }
        InboxDatabase(context).use { it.reconcile(listOf(a, b), emptyList(), emptySet(), 2L, tree, false) }
        InboxRuntime.refresh(context)
        show()
        awaitRow(a); awaitRow(b)
        compose.onNodeWithTag("approval-row-${a.packageId}").performTouchInput { swipeLeft() }
        compose.waitUntil(10_000) { ReviewRuntime.pendingRejects(context).size == 1 }
        val first = ReviewRuntime.pendingRejects(context).single()
        assertEquals(a.packageId, first.packageId)
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("approval-row-${a.packageId}").fetchSemanticsNodes().isEmpty() }
        compose.onNodeWithTag("approval-row-${b.packageId}").performTouchInput { swipeLeft() }
        compose.waitUntil(10_000) { ReviewRuntime.pendingRejects(context).size == 2 }
        val byId = ReviewRuntime.pendingRejects(context).associateBy { it.packageId }
        assertEquals(setOf(a.packageId, b.packageId), byId.keys)
        assertEquals(first.fireAt, byId.getValue(a.packageId).fireAt)
    }

    @Test fun pendingRejectFiresAfterFiveSeconds() {
        val (context, p) = prepare()
        show()
        awaitRow(p)
        compose.onNodeWithTag("approval-row-${p.packageId}").performTouchInput { swipeLeft() }
        compose.waitUntil(10_000) { ReviewRuntime.pendingRejects(context).isNotEmpty() }
        compose.mainClock.advanceTimeBy(4_000)
        compose.waitForIdle()
        assertTrue(ReviewRuntime.records(context).isEmpty())
        compose.mainClock.advanceTimeBy(1_000)
        compose.waitUntil(10_000) { ReviewRuntime.records(context).isNotEmpty() }
        assertEquals(Verdict.Action.REJECT, ReviewRuntime.records(context).single().verdict.action)
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("approval-row-${p.packageId}").fetchSemanticsNodes().isEmpty() }
        assertTrue(ReviewRuntime.pendingRejects(context).isEmpty())
    }

    @Test fun unreadablePackageCannotBeSwipeRejected() {
        val (context, p) = prepare()
        // Newer inbox revision than any stored package: open() fails, so the package is unreadable.
        InboxDatabase(context).use { it.reconcile(listOf(p.copy(revision = p.revision + 1)), emptyList(), emptySet(), 2L, "tree", false) }
        InboxRuntime.refresh(context)
        show()
        awaitRow(p)
        compose.onNodeWithText("문서 확인 필요").assertExists()
        compose.onNodeWithTag("approval-row-${p.packageId}").performTouchInput { swipeLeft() }
        compose.waitForIdle()
        assertTrue(ReviewRuntime.pendingRejects(context).isEmpty())
        compose.onNodeWithText("문서 확인 필요").assertExists()
    }
}
