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
import androidx.compose.ui.test.junit4.StateRestorationTester
import io.github.junhcha.weaver.inbox.InboxDatabase
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.ui.InboxPanel
import androidx.test.core.app.ApplicationProvider
import io.github.junhcha.weaver.MainActivity
import io.github.junhcha.weaver.courier.*
import io.github.junhcha.weaver.schema.*
import io.github.junhcha.weaver.storage.AppStorage
import io.github.junhcha.weaver.ui.ReviewPanel
import io.github.junhcha.weaver.ui.FACT_REVIEW_GUIDANCE
import io.github.junhcha.weaver.ui.INFERRED_REVIEW_LABEL
import io.github.junhcha.weaver.ui.capturedAtLabel
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

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class ReviewPanelTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()

    private fun prepare(transform: (Proposal) -> Proposal = { it }): Pair<Context, Proposal> {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val factory = object : WorkerFactory() {
            override fun createWorker(appContext: Context, workerClassName: String, workerParameters: WorkerParameters): ListenableWorker =
                object : Worker(appContext, workerParameters) {
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
        val p = transform(CourierClient.decoded<Proposal>(File(System.getProperty("weaver.schemaDir"), "examples/Proposal.json").readText()))
        SafCourierFolder(context.contentResolver, tree).publish("proposals", "${p.proposalId}.r${p.rev}.json") {
            it.write(WeaverCodec.encode(p).toByteArray())
        }
        InboxDatabase(context).use { it.reconcile(listOf(p), emptyList(), emptySet(), 1L, tree.toString(), false) }
        InboxRuntime.refresh(context)
        return context to p
    }

    @Test fun readsEvidenceAndAskThenQueuesRejectFromCompose() {
        val (context, p) = prepare()
        compose.runOnUiThread {
            compose.activity.setContent {
                MaterialTheme {
                    Column(Modifier.verticalScroll(rememberScrollState())) { ReviewPanel(p.proposalId) {} }
                }
            }
        }
        compose.waitUntil(10_000) { compose.onAllNodesWithText("제안 r${p.rev} · ${p.summary}").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("근거: ${p.threadExcerpt}").assertExists()
        compose.onAllNodesWithText("확신도", substring = true).assertCountEquals(
            p.entities.size + p.relations.size + p.entryDrafts.size + p.schedules.size + ReviewDraft.questions(p).size)
        compose.onNodeWithText(p.entities.first().ask!!.question, substring = true).assertExists()
        compose.onNodeWithText("거절", useUnmergedTree = true).performScrollTo().performClick()
        compose.waitUntil(10_000) { ReviewRuntime.records(context).isNotEmpty() }
        assertEquals(Verdict.Kind.REJECT, ReviewRuntime.records(context).single().verdict.verdict)
        assertEquals(Verdict.RejectReason.OTHER, ReviewRuntime.records(context).single().verdict.rejectReason)
        compose.waitUntil(10_000) {
            compose.onAllNodesWithText(ReviewRuntime.records(context).single().status.label).fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithText("승인").assertIsNotEnabled()
    }
    @Test fun inboxOpensV2SummaryQuestionsAndQueuesAnswer() {
        val (context, old) = prepare()
        val p = io.github.junhcha.weaver.schema.v2.ReviewPackage(2, old.proposalId, old.source.sourceId,
            old.rev + 1, "요약 문장\n숨겨진 본문", listOf(io.github.junhcha.weaver.schema.v2.Question(
                "선택 질문", "선택 이유", listOf("선택 하나", "선택 둘", "선택 셋"))),
            io.github.junhcha.weaver.schema.v2.Stage.REVIEW, emptyMap())
        val tree = android.net.Uri.parse(context.getSharedPreferences("courier", Context.MODE_PRIVATE).getString("tree", null))
        SafCourierFolder(context.contentResolver, tree).publish("proposals", "${p.packageId}.r${p.revision}.json") {
            it.write(WeaverCodec.encode(p).toByteArray())
        }
        InboxDatabase(context).use { it.reconcile(emptyList(), emptyList(), emptySet(), 2L, tree.toString(), false, listOf(p)) }
        InboxRuntime.refresh(context)
        compose.runOnUiThread { compose.activity.setContent { MaterialTheme {
            Column(Modifier.verticalScroll(rememberScrollState())) { InboxPanel() }
        } } }
        compose.waitUntil(10_000) { compose.onAllNodesWithText("제안 r${p.revision} 검토").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("제안 r${p.revision} 검토").performScrollTo().performClick()
        compose.waitUntil(10_000) { compose.onAllNodesWithText("선택 질문").fetchSemanticsNodes().isNotEmpty() }
        assertTrue(compose.onAllNodesWithText("요약 문장", substring = true).fetchSemanticsNodes().isNotEmpty())
        compose.onNodeWithText("숨겨진 본문", substring = true).assertDoesNotExist()
        compose.onNodeWithText("최종 검토").assertIsNotEnabled()
        compose.onNodeWithText("선택 둘").performScrollTo().performClick()
        compose.onNodeWithText("최종 검토").performScrollTo().performClick()
        compose.onNodeWithText("승인").performScrollTo().performClick()
        compose.waitUntil(10_000) { ReviewRuntime.recordsV2(context).isNotEmpty() }
        assertEquals(listOf("선택 둘"), ReviewRuntime.recordsV2(context).single().verdict.answers)
    }
    @After fun stopWork() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        context.getSharedPreferences("courier", Context.MODE_PRIVATE).edit().clear().commit()
        WorkManager.getInstance(context).cancelAllWork().result.get()
        WorkManagerTestInitHelper.closeWorkDatabase()
    }
    private fun open(p: Proposal) {
        compose.waitUntil(10_000) { compose.onAllNodesWithText("제안 r${p.rev} 검토").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("제안 r${p.rev} 검토").performScrollTo().performClick()
        awaitProposal(p)
    }
    private fun awaitProposal(p: Proposal) {
        compose.waitUntil(10_000) { compose.onAllNodesWithText("제안 r${p.rev} · ${p.summary}").fetchSemanticsNodes().isNotEmpty() }
    }
    private fun fill() {
        expandAdvanced()
        compose.onNodeWithText("최종 값 (JSON)").performScrollTo().performTextReplacement("\"saved summary\"")
        compose.onNodeWithText("수정 추가").performScrollTo().performClick()
        compose.onNodeWithText("JSON Pointer").performScrollTo().performTextReplacement("/schedules/0/title")
        compose.onNodeWithText("최종 값 (JSON)").performScrollTo().performTextReplacement("\"unfinished title\"")
        compose.onNodeWithText("답 (자유 답변은 다시 짜기로 전송)").performScrollTo().performTextReplacement("saved answer")
        compose.onNodeWithText("다시 짤 자연어 지시").performScrollTo().performTextReplacement("saved instruction")
        compose.onNodeWithText("duplicate").performScrollTo().performClick()
    }
    private fun assertInput() {
        compose.onNodeWithText("JSON Pointer").assertTextContains("/schedules/0/title")
        compose.onNodeWithText("최종 값 (JSON)").assertTextContains("\"unfinished title\"")
        compose.onNodeWithText("/summary = \"saved summary\"").assertExists()
        compose.onNodeWithText("답 (자유 답변은 다시 짜기로 전송)").assertTextContains("saved answer")
        compose.onNodeWithText("다시 짤 자연어 지시").assertTextContains("saved instruction")
        compose.onNodeWithText("거절 사유: DUPLICATE").assertExists()
    }
    @Test fun activityRecreationKeepsSelectionRevisionAndAllInputs() {
        val (context, p) = prepare()
        // Use MainActivity's production HomeScreen so recreation installs the same content.
        open(p)
        fill()
        compose.activityRule.scenario.recreate()
        awaitProposal(p)
        assertInput()
        assertTrue(ReviewRuntime.records(context).isEmpty())
        compose.onNodeWithText("수정 제출").assertIsEnabled()
    }
    @Test fun savedRegistryRestorationKeepsDraftAndBlocksNewerRevision() {
        val (context, p) = prepare()
        compose.runOnUiThread {
            compose.activity.findViewById<android.view.ViewGroup>(android.R.id.content).removeAllViews()
        }
        val restoration = StateRestorationTester(compose)
        restoration.setContent {
            MaterialTheme { Column(Modifier.verticalScroll(rememberScrollState())) { InboxPanel() } }
        }
        open(p)
        fill()
        val tree = DocumentsContract.buildTreeDocumentUri("com.android.externalstorage.documents", TestDocumentsProvider.ROOT)
        SafCourierFolder(context.contentResolver, tree).publish("proposals", "${p.proposalId}.r${p.rev + 1}.json") {
            it.write(WeaverCodec.encode(p.copy(rev = p.rev + 1)).toByteArray())
        }
        restoration.emulateSavedInstanceStateRestore()
        awaitProposal(p)
        assertInput()
        compose.waitUntil(10_000) {
            compose.onAllNodesWithText("새 rev 또는 파일 변경 감지 · 이전 rev는 제출할 수 없습니다").fetchSemanticsNodes().isNotEmpty()
        }
        listOf("승인", "수정 제출", "다시 짜기", "거절").forEach { compose.onNodeWithText(it).assertIsNotEnabled() }
        assertTrue(ReviewRuntime.records(context).isEmpty())
        assertTrue(SafCourierFolder(context.contentResolver, tree).list("decisions").isEmpty())
    }
    @Test fun invalidEditShowsFormErrorWithoutLockingRevision() {
        val (context, p) = prepare()
        open(p)
        expandAdvanced()
        compose.onNodeWithText("JSON Pointer").performScrollTo().performTextReplacement("/relations/0/to")
        compose.onNodeWithText("최종 값 (JSON)").performScrollTo().performTextReplacement("\"e999\"")
        compose.onNodeWithText("수정 추가").performScrollTo().performClick()
        compose.onNodeWithText("dangling entity reference").assertExists()
        assertTrue(ReviewRuntime.records(context).isEmpty())
        compose.onNodeWithText("거절").assertIsEnabled()
        compose.onNodeWithText("최종 값 (JSON)").performScrollTo().performTextReplacement("\"e2\"")
        compose.onNodeWithText("수정 추가").performScrollTo().performClick()
        compose.onNodeWithText("/relations/0/to = \"e2\"").assertExists()
    }

    @Test fun submissionValidationFailureCreatesNoVerdictAndAllowsCorrection() {
        val (context, p) = prepare { original ->
            original.copy(schedules = original.schedules.map { it.copy(end = it.start) })
        }
        open(p)
        val choice = p.entities.first().ask!!.choices.first()
        compose.onNodeWithText(choice.label, substring = false).performScrollTo().performClick()
        compose.onNodeWithText("수정 제출").performScrollTo().performClick()
        compose.waitUntil(10_000) {
            compose.onAllNodesWithText("schedule end must follow start").fetchSemanticsNodes().isNotEmpty()
        }
        assertTrue(ReviewRuntime.records(context).isEmpty())
        val tree = DocumentsContract.buildTreeDocumentUri("com.android.externalstorage.documents", TestDocumentsProvider.ROOT)
        assertTrue(SafCourierFolder(context.contentResolver, tree).list("decisions").isEmpty())
        compose.onNodeWithText("수정 제출").assertIsEnabled()
        expandAdvanced()
        compose.onNodeWithText("JSON Pointer").performScrollTo().performTextReplacement("/schedules/0/end")
        compose.onNodeWithText("최종 값 (JSON)").performScrollTo().performTextReplacement("null")
        compose.onNodeWithText("수정 추가").performScrollTo().performClick()
        compose.onNodeWithText("수정 제출").performScrollTo().performClick()
        compose.waitUntil(10_000) { ReviewRuntime.records(context).isNotEmpty() }
        assertEquals(Verdict.Kind.EDIT, ReviewRuntime.records(context).single().verdict.verdict)
    }

    private fun confirmationProposal(): Pair<Context, Proposal> = prepare { p ->
        p.copy(entities = p.entities.map { it.copy(ask = null) },
            entryDrafts = p.entryDrafts.mapIndexed { i, item ->
                if (i == 0) item.copy(ask = Proposal.Ask("Synthetic confirmation", emptyList())) else item
            })
    }

    @Test fun confirmationCanBeCancelledRestoresAcrossRotationAndSubmitsEdit() {
        val (context, p) = confirmationProposal()
        open(p)
        val q = ReviewDraft.questions(p).single()
        compose.onNodeWithText("질문 대상: ${q.summary} · ${q.confidenceLabel}").assertExists()
        compose.onNodeWithText("이 내용이 맞음").performScrollTo().performClick()
        compose.onNodeWithText("확인 취소").performScrollTo().performClick()
        compose.onNodeWithText("승인").performScrollTo().performClick()
        compose.waitUntil(10_000) {
            compose.onAllNodesWithText("미해결 질문 1개:", substring = true).fetchSemanticsNodes().isNotEmpty()
        }
        assertTrue(ReviewRuntime.records(context).isEmpty())
        compose.onNodeWithText("이 내용이 맞음").performScrollTo().performClick()
        compose.activityRule.scenario.recreate()
        awaitProposal(p)
        compose.onNodeWithText("확인 취소").assertExists()
        compose.onNodeWithText("승인").performScrollTo().performClick()
        compose.waitUntil(10_000) { ReviewRuntime.records(context).isNotEmpty() }
        val verdict = ReviewRuntime.records(context).single().verdict
        assertEquals(Verdict.Kind.EDIT, verdict.verdict)
        assertEquals(listOf(Verdict.Edit("/entry_drafts/0/ask", kotlinx.serialization.json.JsonNull)), verdict.edits)
        assertNull(verdict.instruction)
    }

    @Test fun savedConfirmationAndFreeAnswerRestoreAndSubmitRevise() {
        val (context, p) = confirmationProposal()
        compose.runOnUiThread {
            compose.activity.findViewById<android.view.ViewGroup>(android.R.id.content).removeAllViews()
        }
        val restoration = StateRestorationTester(compose)
        restoration.setContent {
            MaterialTheme { Column(Modifier.verticalScroll(rememberScrollState())) { InboxPanel() } }
        }
        open(p)
        compose.onNodeWithText("이 내용이 맞음").performScrollTo().performClick()
        compose.onNodeWithText("답 (자유 답변은 다시 짜기로 전송)").performScrollTo().performTextReplacement("Synthetic correction")
        restoration.emulateSavedInstanceStateRestore()
        awaitProposal(p)
        compose.onNodeWithText("확인 취소").assertExists()
        compose.onNodeWithText("답 (자유 답변은 다시 짜기로 전송)").assertTextContains("Synthetic correction")
        compose.onNodeWithText("승인").performScrollTo().performClick()
        compose.waitUntil(10_000) { ReviewRuntime.records(context).isNotEmpty() }
        val verdict = ReviewRuntime.records(context).single().verdict
        assertEquals(Verdict.Kind.REVISE, verdict.verdict)
        assertTrue(verdict.instruction!!.contains("Synthetic correction"))
        assertTrue(verdict.edits.isEmpty())
    }

    @Test fun displaysQuestionTargetsForEveryGroup() {
        val (_, p) = prepare { original ->
            val ask = Proposal.Ask("Synthetic confirmation", emptyList())
            original.copy(entities = original.entities.map { it.copy(ask = ask) },
                relations = original.relations.map { it.copy(ask = ask) },
                entryDrafts = original.entryDrafts.map { it.copy(ask = ask) },
                schedules = original.schedules.map { it.copy(ask = ask) })
        }
        open(p)
        ReviewDraft.questions(p).forEach { q ->
            compose.onNodeWithText("질문 대상: ${q.summary} · ${q.confidenceLabel}").assertExists()
        }
        compose.onAllNodesWithText("이 내용이 맞음").assertCountEquals(
            p.entities.size + p.relations.size + p.entryDrafts.size)
    }

    @Test fun mixedConfirmationAndFreeAnswerDisplaysWarningAndSubmitsRevise() {
        val (context, p) = prepare { original ->
            original.copy(entities = original.entities.map { it.copy(ask = null) },
                entryDrafts = original.entryDrafts.mapIndexed { i, item ->
                    item.copy(ask = Proposal.Ask("Synthetic question $i", emptyList()))
                })
        }
        open(p)
        val warning = "자유 답변이 있어 다시 짜기로 전송되며, 확인한 항목이 다시 질문될 수 있습니다"
        compose.onNodeWithText(warning).assertDoesNotExist()
        compose.onAllNodesWithText("이 내용이 맞음")[0].performScrollTo().performClick()
        compose.onAllNodesWithText("답 (자유 답변은 다시 짜기로 전송)")[1]
            .performScrollTo().performTextReplacement("Synthetic correction")
        compose.onNodeWithText(warning).performScrollTo().assertExists()
        compose.onNodeWithText("승인").performScrollTo().performClick()
        compose.waitUntil(10_000) { ReviewRuntime.records(context).isNotEmpty() }
        val verdict = ReviewRuntime.records(context).single().verdict
        assertEquals(Verdict.Kind.REVISE, verdict.verdict)
        assertTrue(verdict.edits.isEmpty())
        assertTrue(verdict.instruction!!.contains(ReviewDraft.questions(p).first().summary))
        assertFalse(verdict.instruction.contains("/entry_drafts/0"))
    }

    private fun expandAdvanced() {
        compose.onNodeWithText("고급: 직접 수정").performScrollTo().performClick()
    }

    private fun removeProposal(context: Context, p: Proposal) {
        val tree = DocumentsContract.buildTreeDocumentUri("com.android.externalstorage.documents", TestDocumentsProvider.ROOT)
        DocumentsContract.deleteDocument(context.contentResolver, DocumentsContract.buildDocumentUriUsingTree(tree,
            "${TestDocumentsProvider.ROOT}/proposals/${p.proposalId}.r${p.rev}.json"))
        InboxRuntime.refresh(context)
    }

    @Test fun submittedMissingProposalShowsInferenceWithoutErrorOrStale() {
        val (context, p) = prepare()
        open(p)
        compose.onNodeWithText("거절").performScrollTo().performClick()
        compose.waitUntil(10_000) { ReviewRuntime.records(context).isNotEmpty() }
        val tree = DocumentsContract.buildTreeDocumentUri("com.android.externalstorage.documents", TestDocumentsProvider.ROOT)
        assertTrue(ReviewRuntime.synchronize(context, SafCourierFolder(context.contentResolver, tree)))
        assertEquals(ReviewStatus.SENT, ReviewRuntime.records(context).single().status)
        assertTrue(ReviewRuntime.records(context).single().published)
        removeProposal(context, p)
        compose.waitUntil(10_000) {
            compose.onAllNodesWithText(INFERRED_REVIEW_LABEL).fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithText("제안을 읽을 수 없습니다. 최신 제안을 다시 확인하세요.").assertDoesNotExist()
        compose.onNodeWithText("새 rev 또는 파일 변경 감지 · 이전 rev는 제출할 수 없습니다").assertDoesNotExist()
        compose.onNodeWithText("목록으로 돌아가 최신 제안 확인").assertDoesNotExist()
        compose.onNodeWithText("승인").assertIsNotEnabled()
    }

    private fun assertUnpublishedMissingProposal(status: ReviewStatus) {
        val (context, p) = prepare()
        open(p)
        val tree = DocumentsContract.buildTreeDocumentUri("com.android.externalstorage.documents", TestDocumentsProvider.ROOT)
        val folder = SafCourierFolder(context.contentResolver, tree)
        val store = ReviewStore(File(AppStorage.exchangeDir(context), "reviews"))
        val verdict = ReviewDraft.verdict(p, Verdict.Kind.REJECT, emptyList(),
            Verdict.RejectReason.OTHER, "", emptyMap(), "2026-11-07T00:00:00Z")
        synchronized(CourierRuntime.lock) { store.queue(folder, p, verdict) }
        if (status == ReviewStatus.CONFLICT) {
            folder.publish("decisions", "${p.proposalId}.r${p.rev}.json") {
                it.write("synthetic conflicting decision".toByteArray())
            }
            assertFalse(ReviewRuntime.synchronize(context, folder))
        }
        removeProposal(context, p)
        if (status == ReviewStatus.ERROR) assertFalse(ReviewRuntime.synchronize(context, folder))
        assertEquals(status, ReviewRuntime.records(context).single().status)
        assertFalse(ReviewRuntime.records(context).single().published)
        compose.waitUntil(10_000) {
            compose.onAllNodesWithText("제안을 읽을 수 없습니다. 최신 제안을 다시 확인하세요.").fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithText(INFERRED_REVIEW_LABEL).assertDoesNotExist()
        compose.onNodeWithText("새 rev 또는 파일 변경 감지 · 이전 rev는 제출할 수 없습니다").assertExists()
        compose.onNodeWithText(status.label).assertExists()
        if (status in setOf(ReviewStatus.PENDING, ReviewStatus.ERROR)) {
            compose.onNodeWithText("동일한 검토 결과 재시도").assertExists().assertIsEnabled()
        }
        compose.onNodeWithText("승인").assertIsNotEnabled()
    }

    @Test fun pendingMissingProposalKeepsStatusErrorAndRetry() {
        assertUnpublishedMissingProposal(ReviewStatus.PENDING)
    }

    @Test fun errorMissingProposalKeepsStatusErrorAndRetry() {
        assertUnpublishedMissingProposal(ReviewStatus.ERROR)
    }

    @Test fun conflictMissingProposalKeepsConflictStatus() {
        assertUnpublishedMissingProposal(ReviewStatus.CONFLICT)
    }

    @Test fun collapsedAdvancedEditsKeepSummaryAndApprovalExplanation() {
        val (_, p) = prepare()
        open(p)
        expandAdvanced()
        compose.onNodeWithText("최종 값 (JSON)").performScrollTo().performTextReplacement("\"corrected summary\"")
        compose.onNodeWithText("수정 추가").performScrollTo().performClick()
        compose.onNodeWithText("고급: 직접 수정 접기").performScrollTo().performClick()
        compose.onNodeWithText("JSON Pointer").assertDoesNotExist()
        compose.onNodeWithText("수정 1건 추가됨").performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("수정이 있어 승인이 비활성화되었습니다. 수정 제출을 사용하세요.").assertExists()
        compose.onNodeWithText("승인").assertIsNotEnabled()
        compose.onNodeWithText("수정 제출").assertIsEnabled()
        expandAdvanced()
        compose.onNodeWithText("/summary = \"corrected summary\"").assertExists()
        compose.onNodeWithText("수정 초기화").performScrollTo().performClick()
        compose.onNodeWithText("수정 1건 추가됨").assertDoesNotExist()
        compose.onNodeWithText("수정이 있어 승인이 비활성화되었습니다. 수정 제출을 사용하세요.").assertDoesNotExist()
        compose.onNodeWithText("승인").assertIsEnabled()
    }

    @Test fun unsubmittedMissingProposalKeepsErrorAndStale() {
        val (context, p) = prepare()
        open(p)
        removeProposal(context, p)
        compose.waitUntil(10_000) {
            compose.onAllNodesWithText("제안을 읽을 수 없습니다. 최신 제안을 다시 확인하세요.").fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithText(INFERRED_REVIEW_LABEL).assertDoesNotExist()
        compose.onNodeWithText("새 rev 또는 파일 변경 감지 · 이전 rev는 제출할 수 없습니다").assertExists()
        compose.onNodeWithText("승인").assertIsNotEnabled()
    }

    @Test fun guidanceSectionsDecisionDescriptionsAndAdvancedDisclosure() {
        val (_, p) = prepare()
        open(p)
        compose.onNodeWithText(FACT_REVIEW_GUIDANCE).assertExists()
        compose.onNodeWithText("요청 해소나 일정 실행 허가가 아닙니다.").assertExists()
        listOf("안내", "요약과 근거", "추출 항목", "확인할 질문", "수정", "결정 버튼",
            "승인: 이대로 기록", "수정 제출: 고친 내용으로 기록", "다시 짜기: 지시를 반영해 새 수정본을 받음", "거절: 기록하지 않음")
            .forEach { compose.onNodeWithText(it).assertExists() }
        compose.onNodeWithText("JSON Pointer").assertDoesNotExist()
        expandAdvanced()
        compose.onNodeWithText("JSON Pointer").assertExists()
        compose.activityRule.scenario.recreate()
        awaitProposal(p)
        compose.onNodeWithText("JSON Pointer").assertExists()
        compose.onNodeWithText("고급: 직접 수정 접기").performScrollTo().performClick()
        compose.onNodeWithText("JSON Pointer").assertDoesNotExist()
    }

    @Test fun inboxShowsInformationForEachRequest() {
        val (context, p) = prepare()
        val other = p.copy(proposalId = "prp_01ARZ3NDEKTSV4RRFFQ69G5FAX", rev = p.rev + 1,
            summary = "Synthetic second request", entities = p.entities.map { it.copy(ask = null) })
        val tree = DocumentsContract.buildTreeDocumentUri("com.android.externalstorage.documents", TestDocumentsProvider.ROOT)
        SafCourierFolder(context.contentResolver, tree).publish("proposals", "${other.proposalId}.r${other.rev}.json") {
            it.write(WeaverCodec.encode(other).toByteArray())
        }
        InboxDatabase(context).use { it.reconcile(listOf(p, other), emptyList(), emptySet(), 2L, tree.toString(), false) }
        InboxRuntime.refresh(context)
        compose.waitUntil(10_000) {
            compose.onAllNodesWithText("rev ${other.rev} · 미해결 질문 ${ReviewDraft.questions(other).size}개").fetchSemanticsNodes().isNotEmpty()
        }
        listOf(p, other).forEach { proposal ->
            compose.onNodeWithText(proposal.summary).assertExists()
            compose.onNodeWithText("rev ${proposal.rev} · 미해결 질문 ${ReviewDraft.questions(proposal).size}개").assertExists()
            compose.onNodeWithText("제안 r${proposal.rev} 검토").assertExists()
        }
        compose.onAllNodesWithText("출처: 공유 텍스트 · 시각: ${capturedAtLabel(p.source.capturedAt)}").assertCountEquals(2)
    }

    @Test fun submittedProposalStillDetectsNewRevision() {
        val (context, p) = prepare()
        open(p)
        compose.onNodeWithText("거절").performScrollTo().performClick()
        compose.waitUntil(10_000) { ReviewRuntime.records(context).isNotEmpty() }
        val tree = DocumentsContract.buildTreeDocumentUri("com.android.externalstorage.documents", TestDocumentsProvider.ROOT)
        SafCourierFolder(context.contentResolver, tree).publish("proposals", "${p.proposalId}.r${p.rev + 1}.json") {
            it.write(WeaverCodec.encode(p.copy(rev = p.rev + 1)).toByteArray())
        }
        InboxRuntime.refresh(context)
        compose.waitUntil(10_000) {
            compose.onAllNodesWithText("새 rev 또는 파일 변경 감지 · 이전 rev는 제출할 수 없습니다").fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithText(INFERRED_REVIEW_LABEL).assertDoesNotExist()
        compose.onNodeWithText("승인").assertIsNotEnabled()
    }

    @Test fun submittedUnreadableProposalStillShowsError() {
        val (context, p) = prepare()
        open(p)
        compose.onNodeWithText("거절").performScrollTo().performClick()
        compose.waitUntil(10_000) { ReviewRuntime.records(context).isNotEmpty() }
        val tree = DocumentsContract.buildTreeDocumentUri("com.android.externalstorage.documents", TestDocumentsProvider.ROOT)
        val uri = DocumentsContract.buildDocumentUriUsingTree(tree,
            "${TestDocumentsProvider.ROOT}/proposals/${p.proposalId}.r${p.rev}.json")
        context.contentResolver.openOutputStream(uri, "wt")!!.use { it.write("invalid synthetic JSON".toByteArray()) }
        InboxRuntime.refresh(context)
        compose.waitUntil(10_000) {
            compose.onAllNodesWithText("제안을 읽을 수 없습니다. 최신 제안을 다시 확인하세요.").fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithText(INFERRED_REVIEW_LABEL).assertDoesNotExist()
    }

}
