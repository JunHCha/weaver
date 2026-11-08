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
import io.github.junhcha.weaver.review.*
import io.github.junhcha.weaver.schema.*
import io.github.junhcha.weaver.storage.AppStorage
import kotlinx.serialization.json.Json
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
    private lateinit var record: ReviewRecord
    private lateinit var reviewFile: File
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
                        if (values.getAsString(Events.DESCRIPTION) == selectionArgs!!.single()) addRow(arrayOf(index + 1L))
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
        File(AppStorage.exchangeDir(context), "reviews").deleteRecursively()
        provider = RecordingProvider()
        provider.attachInfo(context, ProviderInfo().apply { authority = CalendarContract.AUTHORITY })
        ShadowContentResolver.registerProviderInternal(CalendarContract.AUTHORITY, provider)
        val p = WeaverCodec.json.decodeFromString<Proposal>(File(System.getProperty("weaver.schemaDir"), "examples/Proposal.json").readText())
        val snapshot = p.copy(entities = p.entities.map { it.copy(ask = null) },
            schedules = p.schedules.map { it.copy(blockedByRequest = null, start = "2026-11-08T00:00:00+09:00") },
            entryDrafts = p.entryDrafts.map { it.copy(blockedByRequest = null) })
        val verdict = Verdict(1, p.proposalId, p.rev, Verdict.Kind.APPROVE, emptyList(), null, null, "2026-11-08T00:00:00Z")
        record = ReviewRecord(verdict, "synthetic", ReviewStatus.INFERRED, published = true, proposal = snapshot)
        reviewFile = File(AppStorage.exchangeDir(context), "reviews/${p.proposalId}.r${p.rev}.json")
        reviewFile.parentFile!!.mkdirs()
        reviewFile.writeText(Json.encodeToString(record))
        CalendarRuntime.select(context, target)
    }
    private fun register() = CalendarRuntime.register(context, record.verdict.proposalId, record.verdict.rev, target)
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
            futures.forEach { assertTrue(it.get(10, TimeUnit.SECONDS).single().contains("등록됨")) }
        } finally { executor.shutdownNow() }
        assertEquals(1, provider.inserts.size)
        val values = provider.inserts.single()
        assertEquals(71L, values.getAsLong(Events.CALENDAR_ID))
        assertEquals(0, values.getAsInteger(Events.ALL_DAY))
        assertEquals(3_600_000L, values.getAsLong(Events.DTEND) - values.getAsLong(Events.DTSTART))
        assertEquals(java.util.TimeZone.getDefault().id, values.getAsString(Events.EVENT_TIMEZONE))
        assertFalse(values.containsKey(Events.RRULE))
        assertFalse(values.containsKey(Events.ORGANIZER))
        assertTrue(values.getAsString(Events.DESCRIPTION).startsWith("Weaver registration "))
        assertTrue(CalendarRuntime.status(context, record.verdict.proposalId, record.verdict.rev).single().contains("등록됨"))
        assertEquals(record, ReviewRuntime.records(context).single())
    }

    @Test fun revokedPermissionKeepsApprovalAndSupportsManualRetry() {
        val before = reviewFile.readText()
        provider.denied = true
        assertThrows(SecurityException::class.java) { background { register() } }
        assertTrue(provider.inserts.isEmpty())
        assertEquals(before, reviewFile.readText())
        provider.denied = false
        assertTrue(background { register() }.single().contains("등록됨"))
        assertEquals(1, provider.inserts.size)
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
