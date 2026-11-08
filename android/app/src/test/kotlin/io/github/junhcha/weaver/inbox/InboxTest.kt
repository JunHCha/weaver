package io.github.junhcha.weaver.inbox

import android.Manifest
import android.app.Application
import android.app.Notification
import android.app.NotificationManager
import androidx.core.app.NotificationCompat
import androidx.test.core.app.ApplicationProvider
import io.github.junhcha.weaver.courier.CourierDatabase
import io.github.junhcha.weaver.courier.CourierWorker
import io.github.junhcha.weaver.courier.MemoryFolder
import io.github.junhcha.weaver.schema.Alert
import io.github.junhcha.weaver.schema.Proposal
import io.github.junhcha.weaver.schema.SharedSchema
import io.github.junhcha.weaver.schema.WeaverCodec
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.io.File
import java.time.Instant
import java.util.TimeZone

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [26, 36])
class InboxTest {
    private lateinit var app: Application
    private lateinit var manager: NotificationManager
    private lateinit var diagnostics: CourierDatabase
    private val folder = MemoryFolder()
    private val previousZone = TimeZone.getDefault()
    private val proposal = WeaverCodec.json.decodeFromString<Proposal>(File(SharedSchema.dir, "examples/Proposal.json").readText())
    private val other = proposal.copy(proposalId = "prp_01ARZ3NDEKTSV4RRFFQ69G5FA1", summary = "두 번째 제안")
    private val alert = WeaverCodec.json.decodeFromString<Alert>(File(SharedSchema.dir, "examples/Alert.json").readText())
    // 09:00 in Asia/Seoul, before the 21:00 reminder.
    private val morning = Instant.parse("2026-11-08T00:00:00Z").toEpochMilli()

    @Before fun setup() {
        TimeZone.setDefault(TimeZone.getTimeZone("Asia/Seoul"))
        app = ApplicationProvider.getApplicationContext()
        File(app.filesDir, "inbox.sqlite3").delete()
        File(app.filesDir, "courier.sqlite3").delete()
        diagnostics = CourierDatabase(app)
        manager = app.getSystemService(NotificationManager::class.java)
        manager.cancelAll()
    }
    @After fun teardown() { diagnostics.close(); TimeZone.setDefault(previousZone) }

    private fun grant() = shadowOf(app).grantPermissions(Manifest.permission.POST_NOTIFICATIONS)
    private fun put(p: Proposal) = folder.put("proposals", "${p.proposalId}.r${p.rev}.json", WeaverCodec.encode(p))
    private fun putAlert(a: Alert = alert) = folder.put("proposals", "alert_${a.alertId}.json", WeaverCodec.encode(a))
    private fun remove(name: String) { folder.files.remove("proposals/$name") }
    private fun sync(now: Long = morning): Boolean = CourierWorker.synchronize(app, folder, diagnostics).also {
        // CourierWorker uses wall time for notifications; re-run with the controlled clock.
        InboxRuntime.refresh(app, now)
    }
    private fun scan(now: Long = morning): Boolean {
        val client = io.github.junhcha.weaver.courier.CourierClient(folder, diagnostics)
        return InboxRuntime.apply(app, folder, client.scan(), diagnostics, now)
    }
    private fun posted(id: Int): Notification? = shadowOf(manager).allNotifications
        .singleOrNull { shadowOf(manager).getNotification(id) === it }
    private fun silent(n: Notification) = n.group != null && n.groupAlertBehavior == Notification.GROUP_ALERT_SUMMARY

    @Test fun repeatedScansAndRestartDoNotRepeatNotification() {
        grant()
        put(proposal)
        assertTrue(scan())
        val first = posted(InboxRuntime.PROPOSAL_NOTIFICATION)!!
        assertFalse(silent(first))
        assertEquals("검토 대기 제안 1건", first.extras.getString(NotificationCompat.EXTRA_TITLE))
        manager.cancel(InboxRuntime.PROPOSAL_NOTIFICATION)
        // Same rev again, also after "restart" (a fresh DB helper reads the persisted history).
        assertTrue(scan(morning + 60_000))
        assertTrue(scan(morning + 120_000))
        assertNull(posted(InboxRuntime.PROPOSAL_NOTIFICATION))
        assertEquals(1, InboxRuntime.proposals(app).size)
    }

    @Test fun onlyHighestRevIsActiveAndNewRevUpdatesSilently() {
        grant()
        put(proposal)
        scan()
        val r2 = proposal.copy(rev = 2, summary = "다시 짠 요약")
        put(r2)
        scan(morning + 1_000)
        val item = InboxRuntime.proposals(app).single()
        assertEquals(2L, item.rev)
        assertTrue(item.revised)
        val n = posted(InboxRuntime.PROPOSAL_NOTIFICATION)!!
        assertTrue(silent(n))
        assertTrue(n.extras.getCharSequenceArray(NotificationCompat.EXTRA_TEXT_LINES)!!.single().contains("다시 짠 제안"))
        // A brand-new proposal_id sounds.
        put(other)
        scan(morning + 2_000)
        assertFalse(silent(posted(InboxRuntime.PROPOSAL_NOTIFICATION)!!))
    }

    @Test fun partialInvalidAndTemporaryFilesDoNotAffectTheList() {
        put(proposal)
        scan()
        val r2 = proposal.copy(rev = 2)
        put(r2)
        scan()
        remove("${proposal.proposalId}.r1.json")
        // r2 becomes a partial write: present but unreadable. Keep r2 active; never fall back or drop.
        folder.put("proposals", "${proposal.proposalId}.r2.json", "{\"schema_version\":1,")
        folder.put("proposals", "~syncthing~${proposal.proposalId}.r3.json.tmp", "partial")
        folder.put("proposals", ".${other.proposalId}.r1.json.0123456789abcdef.tmp", "partial")
        scan()
        assertEquals(listOf(proposal.proposalId to 2L), InboxRuntime.proposals(app).map { it.proposalId to it.rev })
        // The next healthy scan recovers normally.
        put(r2.copy(summary = "정상"))
        scan()
        assertEquals(2L, InboxRuntime.proposals(app).single().rev)
        // An older rev resynced after r2 never downgrades.
        put(proposal)
        scan()
        assertEquals(2L, InboxRuntime.proposals(app).single().rev)
    }

    @Test fun deletedProposalLeavesListAndResyncDoesNotRevive() {
        grant()
        put(proposal)
        scan()
        remove("${proposal.proposalId}.r1.json")
        scan()
        assertTrue(InboxRuntime.proposals(app).isEmpty())
        assertNull(posted(InboxRuntime.PROPOSAL_NOTIFICATION))
        put(proposal)
        scan()
        assertTrue(InboxRuntime.proposals(app).isEmpty())
    }

    @Test fun folderAccessFailureKeepsExistingList() {
        put(proposal)
        putAlert()
        scan()
        folder.deny = true
        assertThrows(SecurityException::class.java) { CourierWorker.synchronize(app, folder, diagnostics) }
        assertEquals(1, InboxRuntime.proposals(app).size)
        assertEquals(1, InboxRuntime.alerts(app).size)
    }

    @Test fun permissionDenialStillUpdatesListThenPostsSilentlyAfterGrant() {
        if (android.os.Build.VERSION.SDK_INT < 33) return
        put(proposal)
        putAlert()
        assertTrue(sync())
        assertEquals(1, InboxRuntime.proposals(app).size)
        assertEquals(1, InboxRuntime.alerts(app).size)
        assertTrue(shadowOf(manager).allNotifications.isEmpty())
        grant()
        InboxRuntime.refresh(app, morning + 1_000)
        assertTrue(silent(posted(InboxRuntime.PROPOSAL_NOTIFICATION)!!))
        assertTrue(silent(posted(InboxRuntime.ALERT_NOTIFICATION)!!))
        // The backlog flag is consumed: the next new proposal sounds again.
        put(other)
        scan(morning + 2_000)
        assertFalse(silent(posted(InboxRuntime.PROPOSAL_NOTIFICATION)!!))
    }

    @Test fun alertIsMarkedBeforeApprovalPrivateAndOpensInbox() {
        grant()
        putAlert()
        scan()
        val n = posted(InboxRuntime.ALERT_NOTIFICATION)!!
        assertEquals("일정 변경 가능 1건 · 승인 전", n.extras.getString(NotificationCompat.EXTRA_TITLE))
        assertTrue(n.extras.getCharSequenceArray(NotificationCompat.EXTRA_TEXT_LINES)!!.single().startsWith("승인 전 · "))
        assertEquals(NotificationCompat.VISIBILITY_PRIVATE, n.visibility)
        assertEquals("일정 변경 가능 1건 · 승인 전", n.publicVersion.extras.getString(NotificationCompat.EXTRA_TITLE))
        assertNull(n.publicVersion.extras.getCharSequence(NotificationCompat.EXTRA_TEXT))
        assertTrue(n.flags and NotificationCompat.FLAG_LOCAL_ONLY != 0)
        assertEquals(InboxRuntime.OPEN_INBOX, shadowOf(n.contentIntent).savedIntent.action)
        assertFalse(n.extras.toString().contains(proposal.threadExcerpt))
        // Confirming (swipe/tap) is not resurrected by later scans.
        manager.cancel(InboxRuntime.ALERT_NOTIFICATION)
        scan(morning + 1_000)
        assertNull(posted(InboxRuntime.ALERT_NOTIFICATION))
        assertEquals(1, InboxRuntime.alerts(app).size)
    }

    @Test fun alertResolvesWhenSeenProposalDisappearsAndFileIsDeleted() {
        put(proposal)
        putAlert()
        scan()
        remove("${proposal.proposalId}.r1.json")
        scan()
        assertTrue(InboxRuntime.alerts(app).isEmpty())
        assertTrue("proposals/alert_${alert.alertId}.json" in folder.deleted)
        // A resynced copy is deleted again and not re-listed.
        putAlert()
        scan()
        assertTrue(InboxRuntime.alerts(app).isEmpty())
        assertFalse(folder.files.containsKey("proposals/alert_${alert.alertId}.json"))
    }

    @Test fun alertWithUnseenProposalStaysUntilUserDismisses() {
        putAlert()
        scan()
        scan()
        assertEquals(1, InboxRuntime.alerts(app).size)
        assertTrue(InboxRuntime.dismissAlert(app, alert.alertId, morning))
        assertFalse(InboxRuntime.dismissAlert(app, alert.alertId, morning))
        assertTrue(InboxRuntime.alerts(app).isEmpty())
        assertTrue(folder.files.containsKey("proposals/alert_${alert.alertId}.json"))
        scan()
        assertFalse(folder.files.containsKey("proposals/alert_${alert.alertId}.json"))
        assertTrue(InboxRuntime.alerts(app).isEmpty())
    }

    @Test fun proposalReminderOncePerDayAfter21AndNotForAlerts() {
        grant()
        put(proposal)
        putAlert()
        scan()
        manager.cancelAll()
        val evening = Instant.parse("2026-11-08T12:30:00Z").toEpochMilli() // 21:30 KST
        InboxRuntime.refresh(app, evening)
        assertFalse(silent(posted(InboxRuntime.PROPOSAL_NOTIFICATION)!!))
        assertNull(posted(InboxRuntime.ALERT_NOTIFICATION))
        manager.cancelAll()
        InboxRuntime.refresh(app, evening + 60_000)
        assertNull(posted(InboxRuntime.PROPOSAL_NOTIFICATION))
    }

    @Test fun summaryShowsAtMostThreeLines() {
        grant()
        (1..4).forEach { put(proposal.copy(proposalId = "prp_01ARZ3NDEKTSV4RRFFQ69G5FB$it", summary = "요약 $it")) }
        scan()
        val n = posted(InboxRuntime.PROPOSAL_NOTIFICATION)!!
        assertEquals(3, n.extras.getCharSequenceArray(NotificationCompat.EXTRA_TEXT_LINES)!!.size)
        assertEquals("검토 대기 제안 4건", n.extras.getString(NotificationCompat.EXTRA_TITLE))
    }

    @Test fun lastSuccessfulScanIsRecorded() {
        assertNull(InboxRuntime.lastScanAt(app))
        scan(morning)
        assertEquals(morning, InboxRuntime.lastScanAt(app))
    }
}
