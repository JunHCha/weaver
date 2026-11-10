package io.github.junhcha.weaver.calendar

import io.github.junhcha.weaver.schema.ApprovedSchedule
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.io.File
import java.io.FileOutputStream
import java.time.OffsetDateTime
import java.util.UUID

@Serializable
data class CalendarTarget(val id: Long, val name: String, val account: String, val type: String) {
    fun sameIdentity(other: CalendarTarget) = id == other.id && account == other.account && type == other.type
}
@Serializable
data class CalendarMapping(
    @SerialName("ref") val threadId: String,
    val calendar: CalendarTarget, val marker: String = "Weaver registration ${UUID.randomUUID()}",
    val uri: String? = null, val rowId: Long? = null,
    val event: CalendarEvent? = null,
    val manualReview: Boolean = false, val needsTarget: Boolean = false,
)
@Serializable
private data class CalendarState(val selected: CalendarTarget? = null, val mappings: List<CalendarMapping> = emptyList())

/** Backup-excluded local journal. A pending marker is fsynced BEFORE crossing the provider boundary.
 * All access is serialized by CalendarRuntime.lock, including recovery and repeated button taps.
 */
class CalendarJournal(private val file: File) {
    // Journals written before approved schedules had their own mapping carry extra keys.
    private val json = Json { encodeDefaults = true; ignoreUnknownKeys = true }
    private fun read() = if (file.exists()) json.decodeFromString<CalendarState>(file.readText()) else CalendarState()
    private fun save(state: CalendarState) {
        check(file.parentFile!!.exists() || file.parentFile!!.mkdirs())
        val temp = File(file.parentFile, ".${UUID.randomUUID()}.tmp")
        try {
            FileOutputStream(temp).use { it.write(json.encodeToString(state).toByteArray()); it.fd.sync() }
            check(temp.renameTo(file)) { "일정 등록 기록 저장 실패" }
        } finally { temp.delete() }
    }
    fun selected() = read().selected
    fun select(target: CalendarTarget) = save(read().copy(selected = target))
    fun mappings() = read().mappings
    fun put(mapping: CalendarMapping) {
        val state = read()
        save(state.copy(mappings = state.mappings.filterNot { it.threadId == mapping.threadId } + mapping))
    }
}

@Serializable
data class CalendarEvent(val title: String, val start: Long, val end: Long, val timezone: String) {
    companion object {
        /** A schedule without an end lasts one hour. */
        fun from(schedule: ApprovedSchedule): CalendarEvent {
            val start = OffsetDateTime.parse(schedule.start).toInstant().toEpochMilli()
            val end = schedule.end?.let { OffsetDateTime.parse(it).toInstant().toEpochMilli() } ?: (start + 3_600_000)
            return CalendarEvent(schedule.title, start, end, schedule.timezone)
        }
    }
}

data class CalendarRow(val uri: String, val id: Long, val calendarId: Long, val event: CalendarEvent?)

interface CalendarProvider {
    fun calendars(): List<CalendarTarget>
    /** An empty list proves absence; query failures MUST throw. Return ALL marker matches. */
    fun find(marker: String): List<CalendarRow>
    fun insert(target: CalendarTarget, marker: String, event: CalendarEvent): Pair<String, Long>
}

class CalendarRegistration(private val journal: CalendarJournal, private val provider: CalendarProvider) {
    /** A thread is registered at most once, independent of later revisions of its schedule. */
    fun register(schedule: ApprovedSchedule, target: CalendarTarget): String {
        if (schedule.retracted) return "${schedule.title}: 철회됨 · 캘린더에서 직접 등록 해제하세요"
        if (schedule.blockedBy != null) return "${schedule.title}: 요청 ${schedule.blockedBy} 미해소 · 등록 차단"
        return try {
            val old = journal.mappings().firstOrNull { it.threadId == schedule.threadId }
            if (old?.uri != null) return "${schedule.title}: 등록됨"
            val selected = provider.calendars().firstOrNull { it.sameIdentity(target) }
                ?: return "캘린더를 다시 선택하세요"
            val pending = old ?: CalendarMapping(schedule.threadId, selected, event = CalendarEvent.from(schedule))
            journal.put(pending)
            // Never infer absence from query errors, or accept a copied DESCRIPTION as ownership.
            val rows = provider.find(pending.marker)
            val found = matching(pending, rows)
            if (found != null) {
                journal.put(pending.copy(uri = found.uri, rowId = found.id))
            } else {
                if (rows.isNotEmpty() || pending.manualReview || pending.event == null ||
                    provider.calendars().none { it.sameIdentity(pending.calendar) })
                    return "${schedule.title}: 수동 확인 필요 · 기존 일정 확인"
                val row = provider.insert(pending.calendar, pending.marker, pending.event)
                journal.put(pending.copy(uri = row.first, rowId = row.second))
            }
            "${schedule.title}: 등록됨"
        } catch (_: Exception) { "${schedule.title}: 등록 실패 · 권한과 캘린더를 확인하고 수동 재시도" }
    }

    private fun matching(pending: CalendarMapping, rows: List<CalendarRow>): CalendarRow? =
        rows.singleOrNull()?.takeIf { pending.event != null && it.calendarId == pending.calendar.id && it.event == pending.event }
}
