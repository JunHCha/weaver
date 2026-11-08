package io.github.junhcha.weaver.inbox

import android.content.Context
import android.content.ContextWrapper
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import io.github.junhcha.weaver.schema.Alert
import io.github.junhcha.weaver.schema.Channel
import io.github.junhcha.weaver.schema.Proposal
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/** Synthetic rows in an isolated files dir; no Courier folder, permission or user data is touched. */
@RunWith(AndroidJUnit4::class)
class InboxDeviceTest {
    private val context = ApplicationProvider.getApplicationContext<Context>()
    private val isolated = object : ContextWrapper(context) {
        override fun getFilesDir(): File = File(context.cacheDir, "inbox-instrumentation").apply { mkdirs() }
    }
    private fun proposal(rev: Long) = Proposal(1, "prp_01ARZ3NDEKTSV4RRFFQ69G5FA0", rev,
        Proposal.SourceSummary("src_01ARZ3NDEKTSV4RRFFQ69G5FA0", Channel.SHARE_TEXT, "2026-11-07T15:32:00+09:00", null),
        "합성 요약 r$rev", "합성 근거", emptyList(), emptyList(), emptyList(), emptyList())
    private val alert = Alert(1, "alt_01ARZ3NDEKTSV4RRFFQ69G5FA0", "prp_01ARZ3NDEKTSV4RRFFQ69G5FA9", "s1",
        "일정이 바뀔 수 있음. 승인 전 정보입니다.", "2026-11-07T15:32:00+09:00")

    @Test fun latestRevAndNotificationHistorySurviveReopen() {
        File(isolated.filesDir, "inbox.sqlite3").delete()
        try {
            InboxDatabase(isolated).use { db ->
                db.reconcile(listOf(proposal(2)), listOf(alert), setOf("prp_01ARZ3NDEKTSV4RRFFQ69G5FA0.r3.json"), 1)
                db.markProposalsNotified(); db.markAlertsNotified()
            }
            InboxDatabase(isolated).use { db ->
                assertEquals(emptyList<String>(), db.reconcile(listOf(proposal(1)), listOf(alert), emptySet(), 2))
                val item = db.proposals().single()
                assertEquals(2L, item.rev)
                assertEquals(2L, item.notifiedRev)
                assertTrue(db.alerts().single().notified)
                assertTrue(db.dismiss(alert.alertId, 3))
                assertEquals(listOf("alert_${alert.alertId}.json"), db.reconcile(emptyList(), listOf(alert), emptySet(), 4))
                assertTrue(db.proposals().isEmpty())
            }
        } finally { File(isolated.filesDir, "inbox.sqlite3").delete() }
    }
}
