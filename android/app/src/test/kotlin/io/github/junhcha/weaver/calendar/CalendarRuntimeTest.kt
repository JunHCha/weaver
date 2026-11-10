package io.github.junhcha.weaver.calendar

import android.content.*
import android.content.pm.ProviderInfo
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.os.Looper
import android.provider.CalendarContract
import android.provider.CalendarContract.Calendars
import android.provider.CalendarContract.Events
import androidx.test.core.app.ApplicationProvider
import io.github.junhcha.weaver.schema.ApprovedSchedule
import io.github.junhcha.weaver.storage.AppStorage
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowContentResolver
import java.io.File
import java.util.concurrent.Callable
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** Synthetic provider only; no device calendars, accounts, Courier tree or user vault. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class CalendarRuntimeTest {
    private lateinit var context: Context
    private lateinit var provider: RecordingProvider
    private val schedule = ApprovedSchedule(2, "package-1", "thread-1", "회의", "2026-11-08T00:00:00+09:00", null,
        "Asia/Seoul", null, "a".repeat(64))
    private val target = CalendarTarget(71, "Synthetic calendar", "synthetic@example.invalid", "LOCAL")

    class RecordingProvider : ContentProvider() {
        val inserts = mutableListOf<ContentValues>()
        var denied = false
        var nullQuery = false
        var lastSelection: String? = null
        var lastArgs: Array<out String>? = null
        override fun onCreate() = true
        override fun query(uri: Uri, projection: Array<out String>?, selection: String?, selectionArgs: Array<out String>?, sortOrder: String?): Cursor? {
            if (denied) throw SecurityException("synthetic denied")
            if (nullQuery) return null
            lastSelection = selection; lastArgs = selectionArgs
            return MatrixCursor(projection).apply {
                if (uri == Calendars.CONTENT_URI) {
                    addRow(arrayOf<Any>(71L, "Synthetic calendar", "synthetic@example.invalid", "LOCAL"))
                } else {
                    inserts.forEachIndexed { index, values ->
                        if (values.getAsString(Events.DESCRIPTION) == selectionArgs!!.single()) addRow(arrayOf<Any?>(index + 1L, values.getAsLong(Events.CALENDAR_ID), values.getAsString(Events.TITLE),
                            values.getAsLong(Events.DTSTART), values.getAsLong(Events.DTEND), values.getAsString(Events.EVENT_TIMEZONE)))
                    }
                }
            }
        }
        override fun insert(uri: Uri, values: ContentValues?): Uri {
            check(Looper.myLooper() != Looper.getMainLooper()) { "provider insertion ran on UI thread" }
            if (denied) throw SecurityException("synthetic denied")
            assertEquals(Events.CONTENT_URI, uri)
            inserts += ContentValues(values)
            return ContentUris.withAppendedId(Events.CONTENT_URI, inserts.size.toLong())
        }
        override fun update(uri: Uri, values: ContentValues?, selection: String?, selectionArgs: Array<out String>?): Int = error("unexpected update")
        override fun delete(uri: Uri, selection: String?, selectionArgs: Array<out String>?): Int = error("unexpected delete")
        override fun getType(uri: Uri): String? = null
    }

    @Before fun prepare() {
        context = ApplicationProvider.getApplicationContext()
        File(AppStorage.exchangeDir(context), "calendar").deleteRecursively()
                provider = RecordingProvider()
        provider.attachInfo(context, ProviderInfo().apply { authority = CalendarContract.AUTHORITY })
        ShadowContentResolver.registerProviderInternal(CalendarContract.AUTHORITY, provider)
        CalendarRuntime.select(context, target)
    }
    private fun register() = CalendarRuntime.register(context, schedule.packageId, schedule.threadId, target) { listOf(schedule) }
    private fun <T> background(block: () -> T): T {
        val executor = Executors.newSingleThreadExecutor()
        try { return executor.submit(Callable { block() }).get(10, TimeUnit.SECONDS) }
        catch (failure: java.util.concurrent.ExecutionException) { throw failure.cause!! }
        finally { executor.shutdownNow() }
    }

    @Test fun concurrentTapsInsertOnceWithContractFieldsAndDurableMapping() {
        val executor = Executors.newFixedThreadPool(4)
        try {
            val futures = (1..8).map { executor.submit(Callable { register() }) }
            futures.forEach { assertTrue(it.get(10, TimeUnit.SECONDS).contains("등록됨")) }
        } finally { executor.shutdownNow() }
        assertEquals(1, provider.inserts.size)
        val values = provider.inserts.single()
        assertEquals(71L, values.getAsLong(Events.CALENDAR_ID))
        assertEquals(0, values.getAsInteger(Events.ALL_DAY))
        assertEquals(3_600_000L, values.getAsLong(Events.DTEND) - values.getAsLong(Events.DTSTART))
        assertEquals("Asia/Seoul", values.getAsString(Events.EVENT_TIMEZONE))
        assertFalse(values.containsKey(Events.RRULE))
        assertFalse(values.containsKey(Events.ORGANIZER))
        assertTrue(values.getAsString(Events.DESCRIPTION).startsWith("Weaver registration "))
        val journal = CalendarJournal(File(AppStorage.exchangeDir(context), "calendar/registrations.json"))
        assertEquals("thread-1", journal.mappings().single().threadId)
        assertNotNull(journal.mappings().single().uri)
    }

    @Test fun unreadableScheduleBlocksRegistration() {
        val message = CalendarRuntime.register(context, "package-1", "missing", target) { listOf(schedule) }
        assertEquals("일정을 다시 읽을 수 없습니다 · 등록 차단", message)
        assertTrue(provider.inserts.isEmpty())
    }

    @Test fun revokedPermissionKeepsStateAndSupportsManualRetry() {
        provider.denied = true
        assertTrue(background { register() }.contains("등록 실패"))
        assertTrue(provider.inserts.isEmpty())
        provider.denied = false
        assertTrue(background { register() }.contains("등록됨"))
        assertEquals(1, provider.inserts.size)
    }

    @Test fun copiedMarkerWithDifferentCalendarAndTimeNeverAutomaticallyInserts() {
        background { register() }
        val journal = CalendarJournal(File(AppStorage.exchangeDir(context), "calendar/registrations.json"))
        val pending = journal.mappings().single().copy(uri = null, rowId = null)
        journal.put(pending)
        val copied = provider.inserts.single()
        copied.put(Events.CALENDAR_ID, 999L)
        copied.put(Events.DTSTART, copied.getAsLong(Events.DTSTART) + 60_000)
        assertTrue(background { register() }.contains("수동 확인"))
        assertNull(journal.mappings().single().uri)
        assertEquals(1, provider.inserts.size) // Only the original fixture, no new writes.
    }

    @Test fun calendarsAreFilteredByWriteAccessAndNullRecoveryCursorThrows() {
        assertEquals(listOf(target), DeviceCalendarProvider(context).calendars())
        assertEquals("${Calendars.CALENDAR_ACCESS_LEVEL} >= ? AND ${Calendars.SYNC_EVENTS} = ?", provider.lastSelection)
        assertArrayEquals(arrayOf(Calendars.CAL_ACCESS_CONTRIBUTOR.toString(), "1"), provider.lastArgs)
        provider.nullQuery = true
        assertThrows(IllegalStateException::class.java) { DeviceCalendarProvider(context).find("synthetic marker") }
        assertTrue(provider.inserts.isEmpty())
    }
}
