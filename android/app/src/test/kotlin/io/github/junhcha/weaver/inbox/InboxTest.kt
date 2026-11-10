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
import io.github.junhcha.weaver.schema.ReviewPackage
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
    private val review = WeaverCodec.json.decodeFromString<ReviewPackage>(File(SharedSchema.dir, "examples/ReviewPackage.json").readText())
    private val other = review.copy(packageId = "pkg_01ARZ3NDEKTSV4RRFFQ69G5FA1", draftBody = "두 번째 문서")
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
    private fun put(p: ReviewPackage) = folder.put("proposals", "${p.packageId}.r${p.revision}.json", WeaverCodec.encode(p))
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

    @Test fun databaseOfAnOlderContractIsDiscardedAndStartsEmpty() {
        val path = File(app.filesDir, "inbox.sqlite3")
        android.database.sqlite.SQLiteDatabase.openOrCreateDatabase(path, null).use { db ->
            db.execSQL("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execSQL("CREATE TABLE proposals (id TEXT PRIMARY KEY)")
            db.execSQL("CREATE TABLE submitted (id TEXT PRIMARY KEY)")
            db.execSQL("CREATE TABLE alerts (id TEXT PRIMARY KEY)")
            db.execSQL("INSERT INTO proposals VALUES ('old')")
            db.version = 2
        }
        InboxDatabase(app).use { db ->
            assertTrue(db.reviews().isEmpty())
            assertTrue(db.alerts().isEmpty())
            db.submitted("pkg_01ARZ3NDEKTSV4RRFFQ69G5FA0", 1)
            db.readableDatabase.rawQuery("SELECT name FROM sqlite_master WHERE name='proposals'", null).use {
                assertFalse(it.moveToFirst())
            }
        }
    }

    @Test fun stageIsPersistedAndSubmittedRevisionIsNotRevived() {
        val name = "${review.packageId}.r${review.revision}.json"
        put(review)
        assertTrue(scan())
        assertEquals("REVIEW", InboxRuntime.reviews(app).single().stage)
        folder.put("proposals", name, WeaverCodec.encode(review.copy(stage = io.github.junhcha.weaver.schema.Stage.APPLY)))
        scan()
        assertEquals("APPLY", InboxRuntime.reviews(app).single().stage)
        InboxRuntime.submitted(app, review.packageId, 1)
        remove(name)
        scan()
        folder.put("proposals", name, WeaverCodec.encode(review))
        scan()
        assertTrue(InboxRuntime.reviews(app).isEmpty())
    }

    @Test fun repeatedScansAndRestartDoNotRepeatNotification() {
        grant()
        put(review)
        assertTrue(scan())
        val first = posted(InboxRuntime.REVIEW_NOTIFICATION)!!
        assertFalse(silent(first))
        assertEquals("검토 대기 문서 1건", first.extras.getString(NotificationCompat.EXTRA_TITLE))
        manager.cancel(InboxRuntime.REVIEW_NOTIFICATION)
        // Same rev again, also after "restart" (a fresh DB helper reads the persisted history).
        assertTrue(scan(morning + 60_000))
        assertTrue(scan(morning + 120_000))
        assertNull(posted(InboxRuntime.REVIEW_NOTIFICATION))
        assertEquals(1, InboxRuntime.reviews(app).size)
    }

    @Test fun onlyHighestRevisionIsActiveAndNewRevisionUpdatesSilently() {
        grant()
        put(review)
        scan()
        val r2 = review.copy(revision = 2, draftBody = "다시 정리한 요약")
        put(r2)
        scan(morning + 1_000)
        val item = InboxRuntime.reviews(app).single()
        assertEquals(2L, item.revision)
        assertTrue(item.revised)
        val n = posted(InboxRuntime.REVIEW_NOTIFICATION)!!
        assertTrue(silent(n))
        assertTrue(n.extras.getCharSequenceArray(NotificationCompat.EXTRA_TEXT_LINES)!!.single().contains("피드백 반영본"))
        // A brand-new package_id sounds.
        put(other)
        scan(morning + 2_000)
        assertFalse(silent(posted(InboxRuntime.REVIEW_NOTIFICATION)!!))
    }

    @Test fun partialInvalidAndTemporaryFilesDoNotAffectTheList() {
        put(review)
        scan()
        val r2 = review.copy(revision = 2)
        put(r2)
        scan()
        remove("${review.packageId}.r1.json")
        // r2 becomes a partial write: present but unreadable. Keep r2 active; never fall back or drop.
        folder.put("proposals", "${review.packageId}.r2.json", "{\"schema_version\":1,")
        folder.put("proposals", "~syncthing~${review.packageId}.r3.json.tmp", "partial")
        folder.put("proposals", ".${other.packageId}.r1.json.0123456789abcdef.tmp", "partial")
        scan()
        assertEquals(listOf(review.packageId to 2L), InboxRuntime.reviews(app).map { it.packageId to it.revision })
        // The next healthy scan recovers normally.
        put(r2.copy(draftBody = "정상"))
        scan()
        assertEquals(2L, InboxRuntime.reviews(app).single().revision)
        // An older rev resynced after r2 never downgrades.
        put(review)
        scan()
        assertEquals(2L, InboxRuntime.reviews(app).single().revision)
    }

    @Test fun deletedPackageLeavesListAndResyncedCopyReturnsSilentlyUnlessSubmitted() {
        grant()
        put(review)
        scan()
        remove("${review.packageId}.r1.json")
        scan()
        assertTrue(InboxRuntime.reviews(app).isEmpty())
        assertNull(posted(InboxRuntime.REVIEW_NOTIFICATION))
        // Temporary loss: the same rev returns without sound.
        put(review)
        scan(morning + 1_000)
        assertEquals(1, InboxRuntime.reviews(app).size)
        posted(InboxRuntime.REVIEW_NOTIFICATION)?.let { assertTrue(silent(it)) }
        // A rev this app decided is not revived by a resynced copy.
        InboxRuntime.submitted(app, review.packageId, 1)
        remove("${review.packageId}.r1.json")
        scan()
        put(review)
        scan()
        assertTrue(InboxRuntime.reviews(app).isEmpty())
    }

    @Test fun folderAccessFailureKeepsExistingList() {
        put(review)
        putAlert()
        scan()
        folder.deny = true
        assertThrows(SecurityException::class.java) { CourierWorker.synchronize(app, folder, diagnostics) }
        assertEquals(1, InboxRuntime.reviews(app).size)
        assertEquals(1, InboxRuntime.alerts(app).size)
    }

    @Test fun permissionDenialStillUpdatesListThenPostsSilentlyAfterGrant() {
        if (android.os.Build.VERSION.SDK_INT < 33) return
        put(review)
        putAlert()
        assertTrue(sync())
        assertEquals(1, InboxRuntime.reviews(app).size)
        assertEquals(1, InboxRuntime.alerts(app).size)
        assertTrue(shadowOf(manager).allNotifications.isEmpty())
        grant()
        InboxRuntime.refresh(app, morning + 1_000)
        assertTrue(silent(posted(InboxRuntime.REVIEW_NOTIFICATION)!!))
        assertTrue(silent(posted(InboxRuntime.ALERT_NOTIFICATION)!!))
        // The backlog flag is consumed: the next new package sounds again.
        put(other)
        scan(morning + 2_000)
        assertFalse(silent(posted(InboxRuntime.REVIEW_NOTIFICATION)!!))
    }

    @Test fun alertIsPrivateAndOpensInbox() {
        grant()
        putAlert()
        scan()
        val n = posted(InboxRuntime.ALERT_NOTIFICATION)!!
        assertEquals("일정 변경 가능 1건", n.extras.getString(NotificationCompat.EXTRA_TITLE))
        assertEquals(alert.message, n.extras.getCharSequenceArray(NotificationCompat.EXTRA_TEXT_LINES)!!.single().toString())
        assertEquals(NotificationCompat.VISIBILITY_PRIVATE, n.visibility)
        assertEquals("일정 변경 가능 1건", n.publicVersion.extras.getString(NotificationCompat.EXTRA_TITLE))
        assertNull(n.publicVersion.extras.getCharSequence(NotificationCompat.EXTRA_TEXT))
        assertTrue(n.flags and NotificationCompat.FLAG_LOCAL_ONLY != 0)
        assertEquals(InboxRuntime.OPEN_INBOX, shadowOf(n.contentIntent).savedIntent.action)
        assertFalse(n.extras.toString().contains(review.draftBody))
        // Confirming (swipe/tap) is not resurrected by later scans.
        manager.cancel(InboxRuntime.ALERT_NOTIFICATION)
        scan(morning + 1_000)
        assertNull(posted(InboxRuntime.ALERT_NOTIFICATION))
        assertEquals(1, InboxRuntime.alerts(app).size)
    }

    @Test fun alertResolvesWhenSeenPackageDisappearsAndFileIsDeleted() {
        put(review)
        putAlert()
        scan()
        remove("${review.packageId}.r1.json")
        scan()
        // One absent scan is only a grace period (a revise may briefly show neither rev).
        assertEquals(1, InboxRuntime.alerts(app).size)
        assertTrue(folder.deleted.isEmpty())
        scan()
        assertTrue(InboxRuntime.alerts(app).isEmpty())
        assertTrue("proposals/alert_${alert.alertId}.json" in folder.deleted)
        // A resynced copy of a resolved alert is neither listed nor deleted again.
        folder.deleted.clear()
        putAlert()
        scan()
        scan()
        assertTrue(InboxRuntime.alerts(app).isEmpty())
        assertTrue(folder.deleted.isEmpty())
    }

    @Test fun alertWithUnseenPackageStaysUntilUserDismisses() {
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

    @Test fun reviewReminderOncePerDayAfter21AndNotForAlerts() {
        grant()
        put(review)
        putAlert()
        scan()
        manager.cancelAll()
        val evening = Instant.parse("2026-11-08T12:30:00Z").toEpochMilli() // 21:30 KST
        InboxRuntime.refresh(app, evening)
        assertFalse(silent(posted(InboxRuntime.REVIEW_NOTIFICATION)!!))
        assertNull(posted(InboxRuntime.ALERT_NOTIFICATION))
        manager.cancelAll()
        InboxRuntime.refresh(app, evening + 60_000)
        assertNull(posted(InboxRuntime.REVIEW_NOTIFICATION))
    }

    @Test fun summaryShowsAtMostThreeLines() {
        grant()
        (1..4).forEach { put(review.copy(packageId = "pkg_01ARZ3NDEKTSV4RRFFQ69G5FB$it", draftBody = "요약 $it")) }
        scan()
        val n = posted(InboxRuntime.REVIEW_NOTIFICATION)!!
        assertEquals(3, n.extras.getCharSequenceArray(NotificationCompat.EXTRA_TEXT_LINES)!!.size)
        assertEquals("검토 대기 문서 4건", n.extras.getString(NotificationCompat.EXTRA_TITLE))
    }

    @Test fun lastSuccessfulScanIsRecorded() {
        assertNull(InboxRuntime.lastScanAt(app))
        scan(morning)
        assertEquals(morning, InboxRuntime.lastScanAt(app))
    }

    private fun alertFile() = folder.files.containsKey("proposals/alert_${alert.alertId}.json")

    @Test fun revisionWithBrieflyMissingRevisionKeepsAlert() {
        put(review); putAlert(); scan(); scan()
        remove("${review.packageId}.r1.json")
        scan()
        put(review.copy(revision = 2))
        scan(); scan()
        assertEquals(1, InboxRuntime.alerts(app).size)
        assertTrue(alertFile())
        assertTrue(folder.deleted.isEmpty())
    }

    @Test fun alertFileTemporarilyMissingReturnsAndIsNeverDeleted() {
        grant()
        putAlert(); scan(); scan()
        val bytes = folder.files.remove("proposals/alert_${alert.alertId}.json")!!
        scan()
        assertTrue(InboxRuntime.alerts(app).isEmpty())
        folder.files["proposals/alert_${alert.alertId}.json"] = bytes
        scan(); scan()
        assertEquals(1, InboxRuntime.alerts(app).size)
        assertTrue(folder.deleted.isEmpty())
        // Returning is not a new alert: no new sound.
        posted(InboxRuntime.ALERT_NOTIFICATION)?.let { assertTrue(silent(it)) }
    }

    @Test fun selectingAnotherFolderAndReturningDeletesNothing() {
        put(review); putAlert(); scan(); scan()
        val original = LinkedHashMap(folder.files)
        folder.files.clear(); folder.root = "memory://empty"
        scan(); scan(); scan()
        folder.files.putAll(original); folder.root = "memory://courier"
        scan(); scan(); scan()
        assertTrue(folder.deleted.isEmpty())
        assertEquals(1, InboxRuntime.reviews(app).size)
        assertEquals(1, InboxRuntime.alerts(app).size)
    }

    @Test fun freshlyCreatedReviewDirectoryIsNotAFullScan() {
        put(review); putAlert(); scan(); scan()
        val original = LinkedHashMap(folder.files)
        folder.files.clear(); folder.createdFolders.add("proposals")
        scan(); scan()
        assertEquals(1, InboxRuntime.reviews(app).size)
        assertEquals(1, InboxRuntime.alerts(app).size)
        folder.createdFolders.clear(); folder.files.putAll(original)
        scan(); scan()
        assertTrue(folder.deleted.isEmpty())
        assertEquals(1, InboxRuntime.alerts(app).size)
    }

    @Test fun newAlertForLongGonePackageWithSameRevisionPresentIsKept() {
        put(review); scan()
        remove("${review.packageId}.r1.json")
        scan(); scan(); scan()
        put(review)
        putAlert()
        scan(); scan()
        assertEquals(1, InboxRuntime.alerts(app).size)
        assertTrue(alertFile())
        assertTrue(folder.deleted.isEmpty())
    }

    @Test fun dismissedAlertIsDeletedOnlyWhenListedInTrustedScan() {
        putAlert(); scan(); scan()
        val bytes = folder.files.remove("proposals/alert_${alert.alertId}.json")!!
        assertTrue(InboxRuntime.dismissAlert(app, alert.alertId, morning))
        scan()
        folder.files["proposals/alert_${alert.alertId}.json"] = bytes
        scan()
        assertFalse(alertFile())
        assertTrue(InboxRuntime.alerts(app).isEmpty())
    }
}
