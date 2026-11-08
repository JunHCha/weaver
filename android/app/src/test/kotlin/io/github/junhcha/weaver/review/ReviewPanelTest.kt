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
import androidx.test.core.app.ApplicationProvider
import io.github.junhcha.weaver.MainActivity
import io.github.junhcha.weaver.courier.*
import io.github.junhcha.weaver.schema.*
import io.github.junhcha.weaver.storage.AppStorage
import io.github.junhcha.weaver.ui.ReviewPanel
import org.junit.Assert.*
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

    @Test fun readsEvidenceAndAskThenQueuesRejectFromCompose() {
        val context = ApplicationProvider.getApplicationContext<Context>()
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
        val p = CourierClient.decoded<Proposal>(File(System.getProperty("weaver.schemaDir"), "examples/Proposal.json").readText())
        SafCourierFolder(context.contentResolver, tree).publish("proposals", "${p.proposalId}.r${p.rev}.json") {
            it.write(WeaverCodec.encode(p).toByteArray())
        }
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
}
