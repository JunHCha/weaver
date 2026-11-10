package io.github.junhcha.weaver.calendar

import android.content.ContentUris
import android.content.ContentValues
import android.content.Context
import android.provider.CalendarContract.Calendars
import android.provider.CalendarContract.Events
import io.github.junhcha.weaver.courier.CourierRuntime
import io.github.junhcha.weaver.courier.CourierClient
import io.github.junhcha.weaver.courier.CourierDatabase
import io.github.junhcha.weaver.schema.ApprovedSchedule
import io.github.junhcha.weaver.storage.AppStorage
import java.io.File

/** Invoked on Dispatchers.IO; no provider work or journal IO runs on the UI thread. */
object CalendarRuntime {
    val lock = Any()
    private fun journal(context: Context) = CalendarJournal(File(AppStorage.exchangeDir(context), "calendar/registrations.json"))
    fun schedules(context: Context, packageId: String) = synchronized(CourierRuntime.lock) {
        CourierDatabase(context).use { db ->
            CourierClient(CourierRuntime.folder(context), db).scan().schedules.filter { it.packageId == packageId }
        }
    }
    /** [read] lists the package's published schedules; tests replace the Courier folder with a fixed list. */
    fun register(context: Context, packageId: String, threadId: String, target: CalendarTarget,
                 read: (String) -> List<ApprovedSchedule> = { schedules(context, it) }): String = synchronized(CourierRuntime.lock) {
        val schedule = read(packageId).singleOrNull { it.threadId == threadId }
            ?: return@synchronized "일정을 다시 읽을 수 없습니다 · 등록 차단"
        synchronized(lock) { CalendarRegistration(journal(context), DeviceCalendarProvider(context)).register(schedule, target) }
    }
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
}
