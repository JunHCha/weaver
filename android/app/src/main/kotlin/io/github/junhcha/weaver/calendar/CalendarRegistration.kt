package io.github.junhcha.weaver.calendar

import io.github.junhcha.weaver.review.ReviewRecord
import io.github.junhcha.weaver.review.ReviewRuntime
import io.github.junhcha.weaver.schema.Proposal
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.io.File
import java.io.FileOutputStream
import java.time.OffsetDateTime
import java.util.UUID

@Serializable
data class CalendarTarget(val id: Long, val name: String, val account: String, val type: String)
@Serializable
data class CalendarMapping(
    val proposalId: String, val rev: Long, val ref: String,
    val calendar: CalendarTarget, val marker: String = "Weaver registration ${UUID.randomUUID()}",
    val uri: String? = null, val rowId: Long? = null,
)
@Serializable
private data class CalendarState(val selected: CalendarTarget? = null, val mappings: List<CalendarMapping> = emptyList())

/** Backup-excluded local journal. A pending marker is fsynced BEFORE crossing the provider boundary.
 * All access is serialized by CalendarRuntime.lock, including recovery and repeated button taps.
 */
class CalendarJournal(private val file: File) {
    private val json = Json { encodeDefaults = true }
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
        save(state.copy(mappings = state.mappings.filterNot {
            it.proposalId == mapping.proposalId && it.rev == mapping.rev && it.ref == mapping.ref
        } + mapping))
    }
}

data class CalendarEvent(val title: String, val start: Long, val end: Long, val timezone: String) {
    companion object {
        fun from(schedule: Proposal.Schedule, timezone: String): CalendarEvent {
            val start = OffsetDateTime.parse(schedule.start).toInstant().toEpochMilli()
            val end = schedule.end?.let { OffsetDateTime.parse(it).toInstant().toEpochMilli() } ?: (start + 3_600_000)
            require(end > start) { "종료 시각은 시작 이후여야 합니다" }
            return CalendarEvent(schedule.title, start, end, timezone)
        }
    }
}

interface CalendarProvider {
    fun calendars(): List<CalendarTarget>
    /** null means a successful query proved absence; query failures MUST throw. */
    fun find(marker: String): Pair<String, Long>?
    fun insert(target: CalendarTarget, marker: String, event: CalendarEvent): Pair<String, Long>
}

class CalendarRegistration(private val journal: CalendarJournal, private val provider: CalendarProvider) {
    companion object {
        fun candidate(record: ReviewRecord): Proposal? {
            if (!record.published) return null
            val original = record.proposal ?: return null // Legacy reviews have no recoverable snapshot.
            return ReviewRuntime.calendarReviewCandidate(original, record.verdict)
        }
    }

    /** On reopening, reconcile committed rows only. Recovery never inserts automatically. */
    fun recover(record: ReviewRecord) {
        val proposal = candidate(record) ?: return
        val allowed = proposal.schedules.filter { it.blockedByRequest == null }.map { it.ref }.toSet()
        journal.mappings().filter {
            it.proposalId == proposal.proposalId && it.rev == proposal.rev && it.ref in allowed && it.uri == null
        }.forEach { pending ->
            provider.find(pending.marker)?.let { row -> journal.put(pending.copy(uri = row.first, rowId = row.second)) }
        }
    }

    /** User-triggered only. Never changes a review or updates an existing Calendar event. */
    fun register(record: ReviewRecord, target: CalendarTarget, timezone: String): List<String> {
        val proposal = candidate(record) ?: return emptyList()
        require(provider.calendars().contains(target)) { "쓰기 가능한 캘린더를 다시 선택하세요" }
        return proposal.schedules.map { schedule ->
            if (schedule.blockedByRequest != null) return@map "${schedule.title}: 요청 미해소 · 등록 차단"
            try {
                val old = journal.mappings().firstOrNull {
                    it.proposalId == proposal.proposalId && it.rev == proposal.rev && it.ref == schedule.ref
                }
                if (old?.uri != null) return@map "${schedule.title}: 등록됨"
                val event = CalendarEvent.from(schedule, timezone)
                val pending = old ?: CalendarMapping(proposal.proposalId, proposal.rev, schedule.ref, target)
                journal.put(pending)
                // Recover an insert committed before a crash or a lost insert response. Never guess
                // absence after an exception/null cursor, or associate another proposal's event.
                val row = provider.find(pending.marker) ?: run {
                    require(pending.calendar in provider.calendars()) { "이전 등록 대상 캘린더를 확인하세요" }
                    provider.insert(pending.calendar, pending.marker, event)
                }
                journal.put(pending.copy(uri = row.first, rowId = row.second))
                "${schedule.title}: 등록됨"
            } catch (_: Exception) {
                "${schedule.title}: 등록 실패 · 권한과 캘린더를 확인하고 수동 재시도"
            }
        }
    }
}
