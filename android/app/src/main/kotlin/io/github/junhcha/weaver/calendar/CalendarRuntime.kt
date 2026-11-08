package io.github.junhcha.weaver.calendar

import android.content.ContentUris
import android.content.ContentValues
import android.content.Context
import android.provider.CalendarContract.Calendars
import android.provider.CalendarContract.Events
import io.github.junhcha.weaver.review.ReviewRuntime
import io.github.junhcha.weaver.storage.AppStorage
import java.io.File
import java.util.TimeZone

/** Invoked on Dispatchers.IO; no provider work or journal IO runs on the UI thread. */
object CalendarRuntime {
    val lock = Any()
    private fun journal(context: Context) = CalendarJournal(File(AppStorage.exchangeDir(context), "calendar/registrations.json"))
    fun status(context: Context, id: String, rev: Long): List<String> = synchronized(lock) {
        journal(context).mappings().filter { it.proposalId == id && it.rev == rev }.map {
            "${it.ref}: " + (if (it.uri != null) "등록됨" else when {
                it.manualReview -> "수동 확인 필요 · 확인 후 새로 등록"
                it.needsTarget -> "이전 대상 사용 불가 · 새 대상 선택 후 새로 등록"
                else -> "시도 중 중단 · 수동 재시도로 복구"
            }) +
                " · ${it.calendar.name} · ${it.calendar.account} · ${it.calendar.type}"
        }
    }
    fun actions(context: Context, id: String, rev: Long) = synchronized(lock) {
        journal(context).mappings().filter { it.proposalId == id && it.rev == rev && it.uri == null && (it.manualReview || it.needsTarget) }
    }
    fun targets(context: Context) = synchronized(lock) { DeviceCalendarProvider(context).calendars() }
    fun selected(context: Context) = synchronized(lock) { journal(context).selected() }
    fun select(context: Context, target: CalendarTarget) = synchronized(lock) {
        require(DeviceCalendarProvider(context).calendars().any { it.sameIdentity(target) })
        journal(context).select(target)
    }
    fun recover(context: Context, id: String, rev: Long) = synchronized(lock) {
        val record = ReviewRuntime.records(context).first { it.verdict.proposalId == id && it.verdict.rev == rev }
        CalendarRegistration(journal(context), DeviceCalendarProvider(context)).recover(record)
    }
    fun register(context: Context, id: String, rev: Long, target: CalendarTarget, restartRef: String? = null): List<String> = synchronized(lock) {
        val record = ReviewRuntime.records(context).first { it.verdict.proposalId == id && it.verdict.rev == rev }
        val journal = journal(context)
        CalendarRegistration(journal, DeviceCalendarProvider(context)).register(record, target, TimeZone.getDefault().id, restartRef)
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
            put(Events.ALL_DAY, 0) // v1 has no all-day flag. Midnight is still a timed event.
            put(Events.DESCRIPTION, marker) // App-owned marker; no SYNC_* or sync-adapter privileges.
            put(Events.HAS_ATTENDEE_DATA, 0)
        }
        val uri = checkNotNull(resolver.insert(Events.CONTENT_URI, values)) { "일정 삽입 응답 없음" }
        return uri.toString() to ContentUris.parseId(uri)
    }
}
