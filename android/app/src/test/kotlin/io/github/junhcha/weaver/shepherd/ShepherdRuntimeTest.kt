package io.github.junhcha.weaver.shepherd

import android.Manifest
import android.app.Application
import android.app.NotificationManager
import android.content.ContentProvider
import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.os.ParcelFileDescriptor
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowContentResolver
import java.time.Instant
import java.time.ZoneId
import java.util.TimeZone

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [26, 36])
class ShepherdRuntimeTest {
    private lateinit var app: Application
    private lateinit var manager: NotificationManager
    @Before fun reset() {
        app = ApplicationProvider.getApplicationContext()
        ShepherdRuntime.activatedCache = null
        app.deleteDatabase("shepherd.sqlite3")
        manager = app.getSystemService(NotificationManager::class.java)
        manager.cancelAll()
    }
    private fun ready(db: ShepherdDatabase, id: Int = 1) {
        db.activate(1_000)
        val row = RecordingMetadata("content://media/external/audio/media/$id", "Person_261108_123456.m4a", 10, 1, 2_000)
        // Keep the same metadata for already known rows, as a full scan would.
        val rows = (1..id).map { row.copy(uri = "content://media/external/audio/media/$it", name = "Person${it}_261108_123456.m4a") }
        db.observe(rows, 3_000, ZoneId.of("Asia/Seoul"))
        db.observe(rows, 33_000, ZoneId.of("Asia/Seoul"))
    }
    @Test fun dismissalIsNotRepeatedNewCallsUpdateAndProcessingCancels() {
        shadowOf(app).grantPermissions(Manifest.permission.POST_NOTIFICATIONS)
        ShepherdDatabase(app).use { db ->
            ready(db)
            val morning = Instant.parse("2026-11-08T00:00:00Z").toEpochMilli()
            ShepherdRuntime.notifyPending(app, db, morning)
            assertEquals(1, shadowOf(manager).allNotifications.size)
            assertEquals(ShepherdRuntime.OPEN_PENDING,
                shadowOf(shadowOf(manager).allNotifications.single().contentIntent).savedIntent.action)
            manager.cancel(30)
            ShepherdRuntime.notifyPending(app, db, morning + 1_000)
            assertTrue(shadowOf(manager).allNotifications.isEmpty())
            ready(db, 2)
            ShepherdRuntime.notifyPending(app, db, morning + 2_000)
            assertEquals(1, shadowOf(manager).allNotifications.size)
            db.pendingCalls().forEach { assertTrue(ShepherdRuntime.markProcessed(app, it.id)) }
            assertTrue(shadowOf(manager).allNotifications.isEmpty())
        }
    }
    @Test fun reducingDismissedSummaryDoesNotResurrectButVisibleSummaryUpdates() {
        shadowOf(app).grantPermissions(Manifest.permission.POST_NOTIFICATIONS)
        ShepherdDatabase(app).use { db ->
            ready(db, 3)
            val morning = Instant.parse("2026-11-08T00:00:00Z").toEpochMilli()
            ShepherdRuntime.notifyPending(app, db, morning)
            db.markProcessed(db.pendingCalls().first().id)
            ShepherdRuntime.notifyPending(app, db, morning + 1_000)
            assertEquals("스크립트 미공유 통화 2건", shadowOf(manager).allNotifications.single().extras.getString("android.title"))
            manager.cancel(30)
            db.markProcessed(db.pendingCalls().first().id)
            ShepherdRuntime.notifyPending(app, db, morning + 2_000)
            assertTrue(shadowOf(manager).allNotifications.isEmpty())
            ShepherdRuntime.notifyPending(app, db, morning + 3_000)
            assertTrue(shadowOf(manager).allNotifications.isEmpty())
        }
    }
    @Test fun removedAndDismissedCallsAreExcludedFromSummary() {
        shadowOf(app).grantPermissions(Manifest.permission.POST_NOTIFICATIONS)
        ShepherdDatabase(app).use { db ->
            ready(db, 2)
            ShepherdRuntime.notifyPending(app, db, 0)
            val calls = db.pendingCalls()
            assertTrue(ShepherdRuntime.dismiss(app, calls.first().id))
            assertFalse(ShepherdRuntime.dismiss(app, calls.first().id))
            db.observe(emptyList(), 40_000, ZoneId.of("UTC"))
            ShepherdRuntime.notifyPending(app, db, 0)
            assertTrue(db.pendingCalls().isEmpty())
            assertTrue(shadowOf(manager).allNotifications.isEmpty())
        }
    }
    @Test fun reindexDoesNotResurrectDismissedNotificationForSameUnsharedCall() {
        shadowOf(app).grantPermissions(Manifest.permission.POST_NOTIFICATIONS)
        ShepherdDatabase(app).use { db ->
            ready(db)
            val morning = Instant.parse("2026-11-08T00:00:00Z").toEpochMilli()
            ShepherdRuntime.notifyPending(app, db, morning)
            manager.cancel(30)
            val row = RecordingMetadata("reindexed", "Person1_261108_123456.m4a", 10, 1, 50_000)
            db.observe(listOf(row), 50_000, ZoneId.of("UTC"))
            ShepherdRuntime.notifyPending(app, db, morning + 1_000)
            assertEquals("reindexed", db.pendingCalls().single().id)
            assertTrue(shadowOf(manager).allNotifications.isEmpty())
        }
    }
    @Test fun reminderOncePerLocalDayAndPermissionDenialDoesNotConsumeNotification() {
        val previousZone = TimeZone.getDefault()
        TimeZone.setDefault(TimeZone.getTimeZone("Asia/Seoul"))
        try {
            ShepherdDatabase(app).use { db ->
                ready(db)
                val morning = Instant.parse("2026-11-08T00:00:00Z").toEpochMilli()
                if (android.os.Build.VERSION.SDK_INT >= 33) {
                    ShepherdRuntime.notifyPending(app, db, morning)
                    assertTrue(shadowOf(manager).allNotifications.isEmpty())
                    assertNull(db.setting("notified_ids"))
                }
                shadowOf(app).grantPermissions(Manifest.permission.POST_NOTIFICATIONS)
                ShepherdRuntime.notifyPending(app, db, morning)
                manager.cancel(30)
                val evening = Instant.parse("2026-11-08T12:00:00Z").toEpochMilli()
                ShepherdRuntime.notifyPending(app, db, evening)
                assertEquals(1, shadowOf(manager).allNotifications.size)
                manager.cancel(30)
                ShepherdRuntime.notifyPending(app, db, evening + 60_000)
                assertTrue(shadowOf(manager).allNotifications.isEmpty())
                ShepherdRuntime.notifyPending(app, db, evening + 86_400_000)
                assertEquals(1, shadowOf(manager).allNotifications.size)
            }
        } finally { TimeZone.setDefault(previousZone) }
    }
    @Test fun scannerOnlyQueriesMetadataAndRevocationStopsQueriesUntilRegrant() {
        val provider = object : ContentProvider() {
            var queries = 0
            override fun onCreate() = true
            override fun query(uri: Uri, projection: Array<out String>?, selection: String?, args: Array<out String>?, sort: String?): Cursor {
                queries++
                assertEquals("content://media/external/audio/media", uri.toString())
                assertTrue(selection!!.contains("date_added >= ? AND date_modified >= ?"))
                assertEquals(args!![0], args[1])
                val path = if (android.os.Build.VERSION.SDK_INT >= 29) "Recordings/Call/" else "/storage/emulated/0/Recordings/Call/a.m4a"
                return MatrixCursor(projection!!).apply {
                    addRow(arrayOf<Any>(1L, "Person_261108_123456.m4a", 100L, 5L, 2L, path))
                }
            }
            override fun openFile(uri: Uri, mode: String): ParcelFileDescriptor = error("Audio must never be opened")
            override fun getType(uri: Uri): String? = null
            override fun insert(uri: Uri, values: ContentValues?): Uri? = error("No media writes")
            override fun delete(uri: Uri, selection: String?, args: Array<out String>?): Int = error("No media writes")
            override fun update(uri: Uri, values: ContentValues?, selection: String?, args: Array<out String>?): Int = error("No media writes")
        }
        ShadowContentResolver.registerProviderInternal("media", provider)
        ShepherdDatabase(app).use { it.activate(1_000) }
        val permission = if (android.os.Build.VERSION.SDK_INT >= 33) Manifest.permission.READ_MEDIA_AUDIO else Manifest.permission.READ_EXTERNAL_STORAGE
        ShepherdRuntime.scan(app, 3_000)
        assertEquals(0, provider.queries)
        shadowOf(app).grantPermissions(permission)
        assertTrue(ShepherdRuntime.scan(app, 3_000))
        shadowOf(app).denyPermissions(permission)
        ShepherdRuntime.scan(app, 20_000)
        assertEquals(1, provider.queries)
        shadowOf(app).grantPermissions(permission)
        assertFalse(ShepherdRuntime.scan(app, 33_000))
        assertEquals(1, ShepherdRuntime.pendingCalls(app).size)
    }
}
