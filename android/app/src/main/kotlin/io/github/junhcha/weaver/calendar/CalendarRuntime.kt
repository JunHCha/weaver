package io.github.junhcha.weaver.calendar

import android.content.ContentUris
import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.provider.CalendarContract.Calendars
import android.provider.CalendarContract.Events
import io.github.junhcha.weaver.courier.CourierClient
import io.github.junhcha.weaver.courier.CourierDatabase
import io.github.junhcha.weaver.schema.Action
import io.github.junhcha.weaver.storage.AppStorage
import java.io.File

/** Invoked on Dispatchers.IO; no provider work or journal IO runs on the UI thread. */
object CalendarRuntime {
    val lock = Any()
    private fun journal(context: Context) = CalendarJournal(File(AppStorage.exchangeDir(context), "calendar/registrations.json"))

    /**
     * Runs the Actions the PC published and reports their final results. Called from the Courier worker, which
     * already holds [CourierRuntime.lock]. Returns false if a result could not be published, so WorkManager retries.
     */
    fun execute(context: Context, actions: List<Action>, client: CourierClient, diagnostics: CourierDatabase,
                provider: CalendarProvider = DeviceCalendarProvider(context)): Boolean {
        if (actions.isEmpty()) return true
        val results = synchronized(lock) { ActionExecutor(journal(context), provider).run(actions) }
        var successful = true
        results.forEach { result ->
            try { client.publishActionResult(result)
            } catch (error: SecurityException) { throw error
            } catch (error: Exception) {
                diagnostics.diagnostic("action-results", "${result.actionId}.json", CourierClient.errorCode(error)); successful = false
            }
        }
        return successful
    }
    fun actions(context: Context) = synchronized(lock) { journal(context).actions() }
    fun targets(context: Context) = synchronized(lock) { DeviceCalendarProvider(context).calendars() }
    fun selected(context: Context) = synchronized(lock) { journal(context).selected() }
    fun select(context: Context, target: CalendarTarget) = synchronized(lock) {
        require(DeviceCalendarProvider(context).calendars().any { it.sameIdentity(target) })
        journal(context).select(target)
    }
}

class DeviceCalendarProvider(context: Context) : CalendarProvider {
    private val resolver = context.contentResolver
    override fun calendars(): List<CalendarTarget> {
        val columns = arrayOf(Calendars._ID, Calendars.CALENDAR_DISPLAY_NAME, Calendars.ACCOUNT_NAME, Calendars.ACCOUNT_TYPE)
        return checkNotNull(resolver.query(Calendars.CONTENT_URI, columns,
            "${Calendars.CALENDAR_ACCESS_LEVEL} >= ? AND ${Calendars.SYNC_EVENTS} = ?",
            arrayOf(Calendars.CAL_ACCESS_CONTRIBUTOR.toString(), "1"), null)) { "캘린더 조회 실패" }.use { cursor ->
            buildList {
                while (cursor.moveToNext()) add(CalendarTarget(cursor.getLong(0), cursor.getString(1).orEmpty(),
                    cursor.getString(2).orEmpty(), cursor.getString(3).orEmpty()))
            }
        }
    }
    override fun find(marker: String): List<CalendarRow> = checkNotNull(resolver.query(
        Events.CONTENT_URI, arrayOf(Events._ID, Events.CALENDAR_ID, Events.TITLE, Events.DTSTART, Events.DTEND, Events.EVENT_TIMEZONE),
        "${Events.DESCRIPTION} = ? AND ${Events.DELETED} = 0",
        arrayOf(marker), null)) { "등록 복구 조회 실패" }.use { cursor ->
        buildList {
            while (cursor.moveToNext()) {
                val id = cursor.getLong(0)
                val event = if ((1..5).any { cursor.isNull(it) }) null else
                    CalendarEvent(cursor.getString(2), cursor.getLong(3), cursor.getLong(4), cursor.getString(5))
                add(CalendarRow(ContentUris.withAppendedId(Events.CONTENT_URI, id).toString(), id, cursor.getLong(1), event))
            }
        }
    }
    override fun insert(target: CalendarTarget, marker: String, event: CalendarEvent): Pair<String, Long> {
        val values = ContentValues().apply {
            put(Events.CALENDAR_ID, target.id)
            put(Events.TITLE, event.title)
            put(Events.DTSTART, event.start)
            put(Events.DTEND, event.end)
            put(Events.EVENT_TIMEZONE, event.timezone)
            put(Events.ALL_DAY, 0) // Schedules have no all-day flag. Midnight is still a timed event.
            put(Events.DESCRIPTION, marker) // App-owned marker; no SYNC_* or sync-adapter privileges.
            put(Events.HAS_ATTENDEE_DATA, 0)
        }
        val uri = checkNotNull(resolver.insert(Events.CONTENT_URI, values)) { "일정 삽입 응답 없음" }
        return uri.toString() to ContentUris.parseId(uri)
    }
    override fun update(uri: String, event: CalendarEvent): Boolean {
        val values = ContentValues().apply {
            put(Events.TITLE, event.title)
            put(Events.DTSTART, event.start)
            put(Events.DTEND, event.end)
            put(Events.EVENT_TIMEZONE, event.timezone)
        }
        return resolver.update(eventUri(uri), values, null, null) > 0
    }
    override fun delete(uri: String): Boolean = resolver.delete(eventUri(uri), null, null) > 0

    /** Only an event row of the calendar provider may be changed, whatever the Action names. */
    private fun eventUri(uri: String): Uri {
        require(Regex("${Regex.escape(Events.CONTENT_URI.toString())}/[0-9]+").matches(uri)) { "not_an_event" }
        return Uri.parse(uri)
    }
}
