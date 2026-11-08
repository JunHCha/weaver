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
        compose.onAllNodesWithText("confidence", substring = true).assertCountEquals(
            p.entities.size + p.relations.size + p.entryDrafts.size + p.schedules.size)
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

}
