package io.github.junhcha.weaver.review

import android.content.Context
import android.content.Intent
import android.content.pm.ProviderInfo
import android.provider.DocumentsContract
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import io.github.junhcha.weaver.inbox.InboxDatabase
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.ui.InboxPanel
import androidx.test.core.app.ApplicationProvider
import io.github.junhcha.weaver.MainActivity
import io.github.junhcha.weaver.courier.*
import io.github.junhcha.weaver.schema.*
import io.github.junhcha.weaver.storage.AppStorage
import io.github.junhcha.weaver.ui.ReviewPanel
import org.junit.Assert.*
import org.junit.After
import androidx.work.*
import androidx.work.testing.SynchronousExecutor
import androidx.work.testing.WorkManagerTestInitHelper
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowContentResolver
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.flow.MutableStateFlow
import io.github.junhcha.weaver.pipeline.PipelineViewModel
import io.github.junhcha.weaver.pipeline.PipelineScreenState

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class ReviewPanelTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()

    private fun prepare(transform: (ReviewPackage) -> ReviewPackage = { it }): Pair<Context, ReviewPackage> {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val factory = object : WorkerFactory() {
            override fun createWorker(appContext: Context, workerClassName: String, workerParameters: WorkerParameters): ListenableWorker =
                if (workerClassName == RejectWorker::class.java.name) RejectWorker(appContext, workerParameters)
                else object : Worker(appContext, workerParameters) {
                    override fun doWork(): Result = Result.success()
                }
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
        val p = transform(CourierClient.decoded<ReviewPackage>(File(System.getProperty("weaver.schemaDir"), "examples/ReviewPackage.json").readText()))
        SafCourierFolder(context.contentResolver, tree).publish("proposals", "${p.packageId}.r${p.revision}.json") {
            it.write(WeaverCodec.encode(p).toByteArray())
        }
        InboxDatabase(context).use { it.reconcile(listOf(p), emptyList(), emptySet(), 1L, tree.toString(), false) }
        InboxRuntime.refresh(context)
        return context to p
    }

    private fun render(content: @androidx.compose.runtime.Composable () -> Unit) {
        compose.runOnUiThread {
            compose.activity.setContent { MaterialTheme { Column(Modifier.verticalScroll(rememberScrollState())) { content() } } }
        }
    }

    @Test fun inboxOpensSummaryQuestionsAndQueuesAnswer() {
        val (context, p) = prepare { it.copy(draftBody = "요약 문장\n숨겨진 본문", questions = listOf(
            Question("선택 질문", "선택 이유", listOf("선택 하나", "선택 둘", "선택 셋")))) }
        render { InboxPanel() }
        compose.waitUntil(10_000) { compose.onAllNodesWithText("문서 검토").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("문서 검토").performScrollTo().performClick()
        compose.waitUntil(10_000) { compose.onAllNodesWithText("선택 질문").fetchSemanticsNodes().isNotEmpty() }
        assertTrue(compose.onAllNodesWithText("요약 문장", substring = true).fetchSemanticsNodes().isNotEmpty())
        compose.onNodeWithText("요약 본문").assertExists()
        compose.onNodeWithText("최종 검토").assertIsNotEnabled()
        compose.onNodeWithText("선택 둘").performScrollTo().performClick()
        compose.onNodeWithText("최종 검토").performScrollTo().performClick()
        compose.onNodeWithText("승인").performScrollTo().performClick()
        compose.waitUntil(10_000) { ReviewRuntime.records(context).isNotEmpty() }
        val verdict = ReviewRuntime.records(context).single().verdict
        assertEquals(listOf("선택 둘"), verdict.answers)
        assertEquals(Verdict.Action.APPROVE, verdict.action)
    }

    @Test fun freeformAnswersAndFeedbackAreSubmittedTogether() {
        val (context, p) = prepare { it.copy(sourceText = "전체 원문", questions = listOf(
            Question("언제 만나나요?", "이유", listOf("오늘", "내일", "모레"), "다음 주 목요일에 만나요"))) }
        render { ReviewPanel(p.packageId) {} }
        compose.waitUntil(10_000) { compose.onAllNodesWithText("직접 답변 1").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("다음 주 목요일에 만나요").assertExists()
        compose.onNodeWithText("직접 답변 1").performScrollTo().performTextReplacement("다음 주 목요일 오후")
        compose.onNodeWithText("최종 검토").performScrollTo().performClick()
        compose.onNodeWithText("추가 피드백 (선택)").performScrollTo().performTextReplacement("장소는 추후 확정")
        compose.onNodeWithText("승인").performScrollTo().performClick()
        compose.waitUntil(10_000) { ReviewRuntime.records(context).isNotEmpty() }
        val verdict = ReviewRuntime.records(context).single().verdict
        assertEquals(listOf("다음 주 목요일 오후"), verdict.answers)
        assertEquals("장소는 추후 확정", verdict.feedback)
    }

    @Test fun feedbackAndEditedSummaryAreSubmittedTogether() {
        val (context, p) = prepare { it.copy(questions = emptyList()) }
        render { ReviewPanel(p.packageId) {} }
        compose.waitUntil(10_000) { compose.onAllNodesWithText("요약 본문").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("요약 본문").performTextReplacement("사람이 수정한 요약")
        compose.onNodeWithText("최종 검토").performScrollTo().performClick()
        compose.onNodeWithText("추가 피드백 (선택)").performScrollTo().performTextReplacement("날짜를 더 확인해 주세요")
        compose.onNodeWithText("수정 제출").performScrollTo().performClick()
        compose.waitUntil(10_000) { ReviewRuntime.records(context).isNotEmpty() }
        val verdict = ReviewRuntime.records(context).single().verdict
        assertEquals(Verdict.Action.EDIT, verdict.action)
        assertEquals("사람이 수정한 요약", verdict.editedBody)
        assertEquals("날짜를 더 확인해 주세요", verdict.feedback)
    }

    @Test fun rejectAndEditNeedTheirOwnInputAndQueueOneVerdict() {
        val (context, p) = prepare { it.copy(questions = emptyList()) }
        render { ReviewPanel(p.packageId) {} }
        compose.waitUntil(10_000) { compose.onAllNodesWithText("최종 검토").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("확인할 질문 없음").assertExists()
        compose.onNodeWithText("최종 검토").performScrollTo().performClick()
        compose.onNodeWithText("요약 본문").performScrollTo().performTextReplacement("고친 본문")
        compose.onNodeWithText("수정 제출").performScrollTo().performClick()
        compose.waitUntil(10_000) { ReviewRuntime.records(context).isNotEmpty() }
        val verdict = ReviewRuntime.records(context).single().verdict
        assertEquals(Verdict.Action.EDIT, verdict.action)
        assertEquals("고친 본문", verdict.editedBody)
        compose.onNodeWithText("수정 제출").assertIsNotEnabled()
        compose.onNodeWithText("거절").assertIsNotEnabled()
    }

    @Test fun topRejectSchedulesWithoutAnswers() {
        val (context, p) = prepare()
        render { ReviewPanel(p.packageId) {} }
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("review-reject").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("직접 답변 1").assertExists()
        compose.onNodeWithTag("review-reject").performScrollTo().assertIsEnabled().performClick()
        compose.waitUntil(10_000) { ReviewRuntime.pendingRejects(context).isNotEmpty() }
        assertEquals(p.packageId, ReviewRuntime.pendingRejects(context).single().packageId)
        assertTrue(ReviewRuntime.records(context).isEmpty())
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("review-undo").fetchSemanticsNodes().isNotEmpty() }
        assertEquals(listOf(p.packageId), ReviewRuntime.flushDueRejects(context, System.currentTimeMillis() + 6_000))
        val verdict = ReviewRuntime.records(context).single().verdict
        assertEquals(Verdict.Action.REJECT, verdict.action)
        assertTrue(verdict.answers.isEmpty())
        assertTrue(ReviewRuntime.pendingRejects(context).isEmpty())
    }

    @Test fun rejectScheduledFromBarePanelIsSentByWorkManager() {
        val (context, p) = prepare()
        render { ReviewPanel(p.packageId) {} }
        // Only a bare panel is on screen: no ApprovalSection exists to send the reject.
        ReviewRuntime.scheduleReject(context, p, System.currentTimeMillis() - 4_900)
        val works = WorkManager.getInstance(context).getWorkInfosForUniqueWork("review-reject-${p.packageId}").get()
        assertEquals(WorkInfo.State.ENQUEUED, works.single().state)
        assertTrue(ReviewRuntime.records(context).isEmpty())
        Thread.sleep(200)
        WorkManagerTestInitHelper.getTestDriver(context)!!.setInitialDelayMet(works.single().id)
        assertEquals(Verdict.Action.REJECT, ReviewRuntime.records(context).single().verdict.action)
        assertTrue(ReviewRuntime.pendingRejects(context).isEmpty())
    }

    @Test fun cancelAfterFireAtIsRefusedAndRejectStillSent() {
        val (context, p) = prepare()
        ReviewRuntime.scheduleReject(context, p, System.currentTimeMillis() - 4_900)
        Thread.sleep(200)
        assertFalse(ReviewRuntime.cancelReject(context, p.packageId))
        assertEquals(listOf(p.packageId), ReviewRuntime.pendingRejects(context).map { it.packageId })
        assertEquals(listOf(p.packageId), ReviewRuntime.flushDueRejects(context))
        assertEquals(Verdict.Action.REJECT, ReviewRuntime.records(context).single().verdict.action)
    }

    @Test fun cancelBeforeFireAtCancelsWork() {
        val (context, p) = prepare()
        ReviewRuntime.scheduleReject(context, p)
        assertTrue(ReviewRuntime.cancelReject(context, p.packageId))
        val works = WorkManager.getInstance(context).getWorkInfosForUniqueWork("review-reject-${p.packageId}").get()
        assertEquals(WorkInfo.State.CANCELLED, works.single().state)
    }

    @Test fun pendingRejectOfAnotherRevisionDoesNotHideQuestions() {
        val (context, p) = prepare()
        ReviewRuntime.scheduleReject(context, p.copy(revision = p.revision + 1))
        render { ReviewPanel(p.packageId) {} }
        compose.waitUntil(10_000) { compose.onAllNodesWithText("직접 답변 1").fetchSemanticsNodes().isNotEmpty() }
        compose.onAllNodesWithTag("review-undo").assertCountEquals(0)
        compose.onAllNodesWithText("거절 예정", substring = true).assertCountEquals(0)
    }

    @Test fun undoInsidePanelKeepsScreenAndDraft() {
        val (_, p) = prepare()
        var submitted = 0
        render { ReviewPanel(p.packageId, onSubmitted = { submitted++ }) {} }
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("review-reject").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("요약 본문").performScrollTo().performTextReplacement("쓰던 본문")
        compose.onNodeWithTag("review-reject").performScrollTo().performClick()
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("review-undo").fetchSemanticsNodes().isNotEmpty() }
        assertEquals(1, submitted)
        compose.onNodeWithTag("review-undo").performScrollTo().performClick()
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("review-reject").fetchSemanticsNodes().isNotEmpty() }
        assertEquals(1, submitted)
        compose.onNodeWithText("쓰던 본문").assertExists()
    }

    @Test fun undoCancelsPendingReject() {
        val (context, p) = prepare()
        ReviewRuntime.scheduleReject(context, p)
        render { ReviewPanel(p.packageId) {} }
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("review-undo").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithTag("review-undo").performScrollTo().performClick()
        compose.waitUntil(10_000) { ReviewRuntime.pendingRejects(context).isEmpty() }
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("review-reject").fetchSemanticsNodes().isNotEmpty() }
    }

    @Test fun failedSendKeepsPendingRejectUntilALaterFlush() {
        val (context, p) = prepare()
        // A different verdict already queued for this revision makes the reject's queue() throw.
        ReviewRuntime.submit(context, p, ReviewDraft.verdict(p, p.questions.map { it.choices[0] }, Verdict.Action.APPROVE, p.draftBody, ""))
        ReviewRuntime.scheduleReject(context, p)
        val now = System.currentTimeMillis() + 6_000
        assertTrue(ReviewRuntime.flushDueRejects(context, now).isEmpty())
        assertEquals(listOf(p.packageId), ReviewRuntime.pendingRejects(context).map { it.packageId })
        File(AppStorage.exchangeDir(context), "reviews").listFiles { f -> f.name.contains(".r${p.revision}.") }!!.forEach { it.delete() }
        assertEquals(listOf(p.packageId), ReviewRuntime.flushDueRejects(context, now))
        assertTrue(ReviewRuntime.pendingRejects(context).isEmpty())
        assertEquals(Verdict.Action.REJECT, ReviewRuntime.records(context).single().verdict.action)
    }

    @Test fun staleRevisionDropsPendingReject() {
        val (context, p) = prepare()
        ReviewRuntime.scheduleReject(context, p)
        val next = p.copy(revision = p.revision + 1)
        val tree = context.getSharedPreferences("courier", Context.MODE_PRIVATE).getString("tree", "")!!
        InboxDatabase(context).use { it.reconcile(listOf(next), emptyList(), emptySet(), 2L, tree, false) }
        assertTrue(ReviewRuntime.flushDueRejects(context, System.currentTimeMillis() + 6_000).isEmpty())
        assertTrue(ReviewRuntime.records(context).isEmpty())
        assertTrue(ReviewRuntime.pendingRejects(context).isEmpty())
    }

    @Test fun answersSurviveActivityRecreation() {
        val (context, p) = prepare()
        assertTrue(io.github.junhcha.weaver.pipeline.PipelineRuntime.snapshot(context).any { it.needsReview })
        val choice = p.questions.single().choices[1]
        // The fixture is installed after Activity startup; refresh its retained model
        // explicitly before driving the real home-to-review navigation.
        val model = androidx.lifecycle.ViewModelProvider(compose.activity)[io.github.junhcha.weaver.pipeline.PipelineViewModel::class.java]
        model.refresh()
        compose.waitUntil(10_000) { model.state.value.items.any { it.needsReview } }
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("approval-row-${p.packageId}").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithTag("approval-row-${p.packageId}").performClick()
        compose.waitUntil(10_000) { compose.onAllNodesWithText(p.questions.single().question).fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText(choice).performScrollTo().performClick()
        compose.onNodeWithText("✓ $choice").assertExists()
        compose.activityRule.scenario.recreate()
        compose.waitUntil(10_000) { compose.onAllNodesWithText("✓ $choice").fetchSemanticsNodes().isNotEmpty() }
    }

    private fun pipelineModel() =
        androidx.lifecycle.ViewModelProvider(compose.activity)[PipelineViewModel::class.java]

    /** Opens the review from its approval row on 지금. */
    private fun openNowReview(p: ReviewPackage) {
        pipelineModel().refresh()
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("approval-row-${p.packageId}").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithTag("approval-row-${p.packageId}").performClick()
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("review-screen").fetchSemanticsNodes().isNotEmpty() &&
            compose.onAllNodesWithText(p.reviewTitle()).fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithTag("now-screen").assertDoesNotExist()
    }

    @Test fun dedicatedReviewSystemAndTopBackReturnToNowAndSurviveRecreation() {
        val (_, p) = prepare { it.copy(questions = emptyList()) }
        openNowReview(p)
        compose.activityRule.scenario.onActivity { it.onBackPressedDispatcher.onBackPressed() }
        compose.onNodeWithTag("now-screen").assertExists()
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("approval-row-${p.packageId}").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithTag("approval-row-${p.packageId}").performClick()
        compose.onNodeWithTag("review-screen").assertExists()
        compose.activityRule.scenario.recreate()
        compose.onNodeWithTag("review-screen").assertExists()
        compose.onNodeWithText("‹ 지금").performClick()
        compose.onNodeWithTag("now-screen").assertExists()
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("approval-row-${p.packageId}").fetchSemanticsNodes().isNotEmpty() }
    }

    private fun submitFromDestination(action: Verdict.Action) {
        val (context, p) = prepare { it.copy(questions = emptyList()) }
        openNowReview(p)
        if (action == Verdict.Action.EDIT)
            compose.onNodeWithText("요약 본문").performTextReplacement("전용 화면에서 수정한 요약")
        compose.onNodeWithText("최종 검토").performScrollTo().performClick()
        val label = when (action) {
            Verdict.Action.APPROVE -> "승인"
            else -> "수정 제출"
        }
        compose.onNodeWithText(label).performScrollTo().performClick()
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("review-screen").fetchSemanticsNodes().isEmpty() }
        compose.onNodeWithTag("now-screen").assertExists()
        compose.onNodeWithTag("approval-row-${p.packageId}").assertDoesNotExist()
        assertEquals(action, ReviewRuntime.records(context).single().verdict.action)
    }

    @Test fun approveReturnsToNowWithoutRow() = submitFromDestination(Verdict.Action.APPROVE)
    @Test fun editReturnsToNowWithoutRow() = submitFromDestination(Verdict.Action.EDIT)

    // Stall only the refresh: verdict IO and its durable queue continue to run normally.
    private fun pauseRefresh(): Pair<PipelineViewModel, Mutex> {
        val model = pipelineModel()
        val mutex = PipelineViewModel::class.java.getDeclaredField("mutex")
            .apply { isAccessible = true }.get(model) as Mutex
        runBlocking { mutex.lock() }
        return model to mutex
    }

    private fun submitApproval() {
        compose.onNodeWithText("최종 검토").performScrollTo().performClick()
        compose.onNodeWithText("승인").performScrollTo().performClick()
        val context = ApplicationProvider.getApplicationContext<Context>()
        compose.waitUntil(10_000) { ReviewRuntime.records(context).isNotEmpty() }
        compose.waitForIdle()
    }

    @Test fun failedRefreshStillReturnsAfterSubmission() {
        val (_, p) = prepare { it.copy(questions = emptyList()) }
        openNowReview(p)
        val (model, mutex) = pauseRefresh()
        try {
            submitApproval()
            // Deliver the same error state that a failed refresh publishes, without corrupting user storage.
            @Suppress("UNCHECKED_CAST")
            val state = PipelineViewModel::class.java.getDeclaredField("mutable")
                .apply { isAccessible = true }.get(model) as MutableStateFlow<PipelineScreenState>
            compose.runOnUiThread { state.value = state.value.copy(error = "합성 갱신 실패") }
            compose.onNodeWithTag("review-screen").assertDoesNotExist()
            compose.onNodeWithTag("now-screen").assertExists()
        } finally {
            mutex.unlock()
        }
    }

    @Test fun stalledRefreshTimesOutAndReturnsToNow() {
        val (_, p) = prepare { it.copy(questions = emptyList()) }
        openNowReview(p)
        val (_, mutex) = pauseRefresh()
        try {
            submitApproval()
            compose.onNodeWithTag("review-screen").assertExists()
            compose.mainClock.advanceTimeBy(3_500)
            compose.onNodeWithTag("review-screen").assertDoesNotExist()
            compose.onNodeWithTag("now-screen").assertExists()
        } finally {
            mutex.unlock()
        }
    }

    @Test fun leavingPendingRefreshDoesNotCloseLaterScreens() {
        val (_, p) = prepare { it.copy(questions = emptyList()) }
        openNowReview(p)
        val (model, mutex) = pauseRefresh()
        try {
            submitApproval()
            compose.onNodeWithText("‹ 지금").performClick()
            compose.onNodeWithTag("now-screen").assertExists()
            compose.onNodeWithText("수집·설정").performClick()
        } finally {
            mutex.unlock()
        }
        compose.waitUntil(10_000) { model.state.value.items.none { it.packageId == p.packageId && it.needsReview } }
        compose.mainClock.advanceTimeBy(3_500)
        compose.onNodeWithText("‹ 지금").assertExists()
    }

    @Test fun submittedRefreshWaitSurvivesActivityRecreationAndStillReturns() {
        val (_, p) = prepare { it.copy(questions = emptyList()) }
        openNowReview(p)
        val (_, mutex) = pauseRefresh()
        try {
            submitApproval()
            compose.onNodeWithTag("review-screen").assertExists()
            compose.activityRule.scenario.recreate()
            compose.onNodeWithTag("review-screen").assertExists()
            compose.mainClock.advanceTimeBy(3_500)
            compose.onNodeWithTag("review-screen").assertDoesNotExist()
            compose.onNodeWithTag("now-screen").assertExists()
        } finally {
            mutex.unlock()
        }
    }

    @Test fun submittingConsumesSystemBackAndDisablesTopBackUntilQueued() {
        val (context, p) = prepare { it.copy(questions = emptyList()) }
        openNowReview(p)
        compose.onNodeWithText("최종 검토").performScrollTo().performClick()
        val acquired = CountDownLatch(1)
        val release = CountDownLatch(1)
        val blocker = thread {
            synchronized(CourierRuntime.lock) {
                acquired.countDown()
                check(release.await(10, TimeUnit.SECONDS))
            }
        }
        try {
            assertTrue(acquired.await(10, TimeUnit.SECONDS))
            compose.onNodeWithText("승인").performScrollTo().performClick()
            compose.onNodeWithText("‹ 지금").assertIsNotEnabled().performClick()
            compose.activityRule.scenario.onActivity { it.onBackPressedDispatcher.onBackPressed() }
            compose.onNodeWithTag("review-screen").assertExists()
        } finally {
            release.countDown()
            blocker.join(10_000)
        }
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("review-screen").fetchSemanticsNodes().isEmpty() }
        assertEquals(1, ReviewRuntime.records(context).size)
        compose.onNodeWithTag("now-screen").assertExists()
    }

    @Test fun missingPackageShowsReadErrorAndKeepsApprovalDisabled() {
        val (context, p) = prepare()
        render { ReviewPanel(p.packageId) {} }
        compose.waitUntil(10_000) { compose.onAllNodesWithText(p.questions.single().question).fetchSemanticsNodes().isNotEmpty() }
        val tree = DocumentsContract.buildTreeDocumentUri("com.android.externalstorage.documents", TestDocumentsProvider.ROOT)
        val uri = DocumentsContract.buildDocumentUriUsingTree(tree,
            "${TestDocumentsProvider.ROOT}/proposals/${p.packageId}.r${p.revision}.json")
        DocumentsContract.deleteDocument(context.contentResolver, uri)
        InboxRuntime.refresh(context)
        compose.waitUntil(10_000) {
            compose.onAllNodesWithText("문서를 읽을 수 없습니다. 최신 문서를 확인하세요.").fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithText("최신 문서를 다시 확인하세요").assertExists()
        compose.onNodeWithText("최종 검토").assertIsNotEnabled()
    }

    @After fun stopWork() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        context.getSharedPreferences("courier", Context.MODE_PRIVATE).edit().clear().commit()
        WorkManager.getInstance(context).cancelAllWork().result.get()
        WorkManagerTestInitHelper.closeWorkDatabase()
    }
}
