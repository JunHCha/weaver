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
import io.github.junhcha.weaver.courier.CourierClient
import io.github.junhcha.weaver.courier.CourierDatabase
import io.github.junhcha.weaver.courier.CourierRuntime
import io.github.junhcha.weaver.courier.MemoryFolder
import io.github.junhcha.weaver.schema.Action
import io.github.junhcha.weaver.schema.ActionKind
import io.github.junhcha.weaver.schema.ActionResult
import io.github.junhcha.weaver.schema.CalendarPayload
import io.github.junhcha.weaver.schema.DecodeResult
import io.github.junhcha.weaver.schema.WeaverCodec
import io.github.junhcha.weaver.storage.AppStorage
import org.junit.Assert.*
import org.junit.After
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
    private val target = CalendarTarget(71, "Synthetic calendar", "synthetic@example.invalid", "LOCAL")
    private val actionId = "act_" + "1".repeat(32)
    private val register = Action(2, actionId, ActionKind.REGISTER, "thread-1", 1, 1,
        CalendarPayload("2026-11-08T00:00:00+09:00", null, "Asia/Seoul", "회의"), null)
    private lateinit var folder: MemoryFolder
    private lateinit var database: CourierDatabase

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
        val updates = mutableListOf<Pair<Uri, ContentValues>>()
        val deletes = mutableListOf<Uri>()
        override fun update(uri: Uri, values: ContentValues?, selection: String?, selectionArgs: Array<out String>?): Int {
            check(Looper.myLooper() != Looper.getMainLooper()) { "provider update ran on UI thread" }
            if (denied) throw SecurityException("synthetic denied")
            updates += uri to ContentValues(values); return 1
        }
        override fun delete(uri: Uri, selection: String?, selectionArgs: Array<out String>?): Int {
            if (denied) throw SecurityException("synthetic denied")
            deletes += uri; return 1
        }
        override fun getType(uri: Uri): String? = null
    }

    @Before fun prepare() {
        context = ApplicationProvider.getApplicationContext()
        File(AppStorage.exchangeDir(context), "calendar").deleteRecursively()
        provider = RecordingProvider()
        provider.attachInfo(context, ProviderInfo().apply { authority = CalendarContract.AUTHORITY })
        ShadowContentResolver.registerProviderInternal(CalendarContract.AUTHORITY, provider)
        folder = MemoryFolder()
        database = CourierDatabase(context)
        CalendarRuntime.select(context, target)
    }
    @After fun close() { database.close() }

    /** The Courier worker holds this lock around every pass. */
    private fun execute(vararg actions: Action): Boolean = synchronized(CourierRuntime.lock) {
        CalendarRuntime.execute(context, actions.toList(), CourierClient(folder, database), database)
    }
    private fun reported(id: String = actionId) =
        (WeaverCodec.decode<ActionResult>(folder.files.getValue("action-results/$id.json").decodeToString()) as DecodeResult.Ok).value
    private fun <T> background(block: () -> T): T {
        val executor = Executors.newSingleThreadExecutor()
        try { return executor.submit(Callable { block() }).get(10, TimeUnit.SECONDS) }
        catch (failure: java.util.concurrent.ExecutionException) { throw failure.cause!! }
        finally { executor.shutdownNow() }
    }
    private fun journal() = CalendarJournal(File(AppStorage.exchangeDir(context), "calendar/registrations.json"))

    @Test fun concurrentPassesInsertOnceWithContractFieldsAndReportTheResult() {
        val executor = Executors.newFixedThreadPool(4)
        try {
            val futures = (1..8).map { executor.submit(Callable { execute(register) }) }
            futures.forEach { assertTrue(it.get(10, TimeUnit.SECONDS)) }
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
        assertEquals(actionId, journal().mappings().single().threadId)
        assertNotNull(journal().mappings().single().uri)
        val result = reported()
        assertEquals(ActionResult.Outcome.COMPLETED, result.outcome)
        assertEquals(journal().mappings().single().uri, result.externalId)
        assertEquals(listOf("$actionId.json"), folder.published)
    }

    @Test fun updateAndRemoveReachTheCalendarProviderOffTheUiThread() {
        assertTrue(background { execute(register) })
        val event = checkNotNull(reported().externalId)
        val moved = register.copy(actionId = "act_" + "2".repeat(32), kind = ActionKind.UPDATE, seq = 2, externalId = event,
            payload = register.payload.copy(start = "2026-11-09T00:00:00+09:00"))
        assertTrue(background { execute(moved) })
        val (uri, values) = provider.updates.single()
        assertEquals(event, uri.toString())
        assertEquals(CalendarEvent.from(moved.payload).start, values.getAsLong(Events.DTSTART))
        val gone = register.copy(actionId = "act_" + "3".repeat(32), kind = ActionKind.REMOVE, seq = 3, externalId = event)
        assertTrue(background { execute(gone) })
        assertEquals(event, provider.deletes.single().toString())
        assertEquals(ActionResult.Outcome.COMPLETED, reported(gone.actionId).outcome)
        assertTrue(journal().mappings().single().removed)
    }

    @Test fun onlyCalendarEventRowsCanBeChangedWhateverTheActionNames() {
        val outsider = register.copy(kind = ActionKind.REMOVE, externalId = "content://contacts/people/1")
        assertTrue(background { execute(outsider) })
        assertTrue(provider.deletes.isEmpty())
        assertEquals(ActionPhase.RETRY, journal().action(outsider.actionId)?.phase)
        assertFalse(folder.files.containsKey("action-results/${outsider.actionId}.json"))
    }

    @Test fun withoutASelectedCalendarTheActionWaitsAndNothingIsReported() {
        File(AppStorage.exchangeDir(context), "calendar").deleteRecursively()
        assertTrue(background { execute(register) })
        assertTrue(provider.inserts.isEmpty())
        assertTrue(folder.published.isEmpty())
        assertEquals("target", journal().action(actionId)?.note)
        assertEquals(listOf(actionId), CalendarRuntime.actions(context).map { it.actionId })
        CalendarRuntime.select(context, target)
        assertTrue(background { execute(register) })
        assertEquals(1, provider.inserts.size)
        assertEquals(ActionResult.Outcome.COMPLETED, reported().outcome)
    }

    @Test fun revokedPermissionKeepsStateAndRunsOnceGranted() {
        provider.denied = true
        assertTrue(background { execute(register) })
        assertTrue(provider.inserts.isEmpty())
        assertTrue(folder.published.isEmpty())
        assertEquals("permission", journal().action(actionId)?.note)
        provider.denied = false
        assertTrue(background { execute(register) })
        assertEquals(1, provider.inserts.size)
    }

    @Test fun anUnpublishableResultIsRetriedWithoutRunningTheActionAgain() {
        folder.failName = "$actionId.json"
        assertFalse(background { execute(register) })
        assertEquals(1, provider.inserts.size)
        folder.failName = null
        assertTrue(background { execute(register) })
        assertEquals(1, provider.inserts.size)
        assertEquals(ActionResult.Outcome.COMPLETED, reported().outcome)
    }

    @Test fun copiedMarkerWithDifferentCalendarAndTimeNeverAutomaticallyInserts() {
        background { execute(register) }
        val journal = journal()
        val pending = journal.mappings().single().copy(uri = null, rowId = null)
        journal.put(pending)
        journal.put(ActionState(actionId, ActionKind.REGISTER, "회의"))
        folder.files.remove("action-results/$actionId.json")
        val copied = provider.inserts.single()
        copied.put(Events.CALENDAR_ID, 999L)
        copied.put(Events.DTSTART, copied.getAsLong(Events.DTSTART) + 60_000)
        assertTrue(background { execute(register) })
        assertEquals("manual_review", reported().error)
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
