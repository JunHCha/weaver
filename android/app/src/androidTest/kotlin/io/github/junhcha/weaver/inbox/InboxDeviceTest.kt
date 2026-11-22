package io.github.junhcha.weaver.inbox

import android.content.Context
import android.content.ContextWrapper
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import io.github.junhcha.weaver.schema.Alert
import io.github.junhcha.weaver.schema.ReviewPackage
import io.github.junhcha.weaver.schema.Stage
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
    private fun review(revision: Long) = ReviewPackage(2, "pkg_0123456789abcdef", "src_01ARZ3NDEKTSV4RRFFQ69G5FA0",
        revision, "합성 요약 r$revision", emptyList(), Stage.REVIEW, emptyMap())
    private val alert = Alert(2, "alt_01ARZ3NDEKTSV4RRFFQ69G5FA0", "pkg_0123456789abcdef", "thr_0123456789abcdef",
        "일정의 시각이 바뀌었을 수 있음.", "2026-11-07T15:32:00+09:00")

    @Test fun latestRevisionAndNotificationHistorySurviveReopen() {
        File(isolated.filesDir, "inbox.sqlite3").delete()
        try {
            InboxDatabase(isolated).use { db ->
                db.reconcile(listOf(review(2)), listOf(alert), setOf("pkg_0123456789abcdef.r3.json"), 1, "tree", false)
                db.markReviewsNotified(); db.markAlertsNotified()
            }
            InboxDatabase(isolated).use { db ->
                assertEquals(emptyList<String>(), db.reconcile(listOf(review(1)), listOf(alert), emptySet(), 2, "tree", false))
                val item = db.reviews().single()
                assertEquals(2L, item.revision)
                assertEquals(2L, item.notifiedRevision)
                assertTrue(db.alerts().single().notified)
                assertTrue(db.dismiss(alert.alertId, 3))
                assertEquals(listOf("alert_${alert.alertId}.json"), db.reconcile(emptyList(), listOf(alert), emptySet(), 4, "tree", false))
                assertTrue(db.reviews().isEmpty())
                // A different tree only adds: nothing is deleted there.
                assertEquals(emptyList<String>(), db.reconcile(emptyList(), listOf(alert), emptySet(), 5, "other", false))
            }
        } finally { File(isolated.filesDir, "inbox.sqlite3").delete() }
    }

    /** Phone acceptance with synthetic data: HTTP -> files replay, close cleanup and unread retention. */
    @Test fun httpToFilesReplayKeepsUnreadAndDoesNotNotifyPostedOrClosedAgain() {
        File(isolated.filesDir, "inbox.sqlite3").delete()
        val server = "synthetic-server"
        val closed = alert.copy(alertId = "alt_01ARZ3NDEKTSV4RRFFQ69G5FA1")
        try {
            InboxDatabase(isolated).use { db ->
                db.bindAlertSource("synthetic-saf", server)
                db.reconcile(emptyList(), listOf(alert, closed), emptySet(), 1, "http:$server", false, serverId = server)
                db.markAlertsNotified()
                db.dismiss(closed.alertId, 2, server)
                db.alertAcknowledged(server, "device-one", alert.alertId)
            }
            // Reopen between ledger commit and adapter cleanup, as after process death.
            InboxDatabase(isolated).use { db ->
                repeat(2) {
                    assertEquals(listOf("alert_${closed.alertId}.json"),
                        db.reconcile(emptyList(), listOf(alert, closed), emptySet(), 3, "synthetic-saf", false))
                    assertEquals(alert.alertId, db.alerts().single().alertId)
                    assertEquals(alert.message, db.alerts().single().message)
                    assertTrue(db.alerts().single().notified)
                }
                db.reconcile(emptyList(), emptyList(), emptySet(), 4, "synthetic-saf", false)
                assertEquals(alert.message, db.alerts().single().message)
                assertEquals(listOf(closed.alertId), db.pendingAlertAcks(server, "device-one"))
                assertEquals(2, db.pendingAlertAcks(server, "new-pairing-device").size)
                db.reconcile(emptyList(), listOf(alert), emptySet(), 5, "http:other-server", false, serverId = "other-server")
                assertFalse(db.alerts().single().notified)
                db.reconcile(emptyList(), emptyList(), emptySet(), 6, "synthetic-saf", false)
                assertTrue(db.alerts().single().notified)
            }
        } finally { File(isolated.filesDir, "inbox.sqlite3").delete() }
    }
}
