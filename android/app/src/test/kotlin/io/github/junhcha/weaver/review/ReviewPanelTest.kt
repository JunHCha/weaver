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

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class ReviewPanelTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()

    private fun prepare(transform: (ReviewPackage) -> ReviewPackage = { it }): Pair<Context, ReviewPackage> {
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
        compose.waitUntil(10_000) { compose.onAllNodesWithText("문서 r${p.revision} 검토").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("문서 r${p.revision} 검토").performScrollTo().performClick()
        compose.waitUntil(10_000) { compose.onAllNodesWithText("선택 질문").fetchSemanticsNodes().isNotEmpty() }
        assertTrue(compose.onAllNodesWithText("요약 문장", substring = true).fetchSemanticsNodes().isNotEmpty())
        compose.onNodeWithText("숨겨진 본문", substring = true).assertDoesNotExist()
        compose.onNodeWithText("최종 검토").assertIsNotEnabled()
        compose.onNodeWithText("선택 둘").performScrollTo().performClick()
        compose.onNodeWithText("최종 검토").performScrollTo().performClick()
        compose.onNodeWithText("승인").performScrollTo().performClick()
        compose.waitUntil(10_000) { ReviewRuntime.records(context).isNotEmpty() }
        val verdict = ReviewRuntime.records(context).single().verdict
        assertEquals(listOf("선택 둘"), verdict.answers)
        assertEquals(Verdict.Action.APPROVE, verdict.action)
    }

    @Test fun rejectAndEditNeedTheirOwnInputAndQueueOneVerdict() {
        val (context, p) = prepare { it.copy(questions = emptyList()) }
        render { ReviewPanel(p.packageId) {} }
        compose.waitUntil(10_000) { compose.onAllNodesWithText("최종 검토").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("확인할 질문 없음").assertExists()
        compose.onNodeWithText("최종 검토").performScrollTo().performClick()
        compose.onNodeWithText("본문 직접 수정").performScrollTo().performClick()
        compose.onNodeWithText("직접 수정할 본문").performScrollTo().performTextReplacement("고친 본문")
        compose.onNodeWithText("수정 제출").performScrollTo().performClick()
        compose.waitUntil(10_000) { ReviewRuntime.records(context).isNotEmpty() }
        val verdict = ReviewRuntime.records(context).single().verdict
        assertEquals(Verdict.Action.EDIT, verdict.action)
        assertEquals("고친 본문", verdict.editedBody)
        compose.onNodeWithText("승인").assertIsNotEnabled()
        compose.onNodeWithText("거절").assertIsNotEnabled()
    }

    @Test fun answersSurviveActivityRecreation() {
        val (_, p) = prepare()
        val choice = p.questions.single().choices[1]
        compose.waitUntil(10_000) { compose.onAllNodesWithText("문서 r${p.revision} 검토").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("문서 r${p.revision} 검토").performScrollTo().performClick()
        compose.waitUntil(10_000) { compose.onAllNodesWithText(p.questions.single().question).fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText(choice).performScrollTo().performClick()
        compose.onNodeWithText("✓ $choice").assertExists()
        compose.activityRule.scenario.recreate()
        compose.waitUntil(10_000) { compose.onAllNodesWithText("✓ $choice").fetchSemanticsNodes().isNotEmpty() }
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
