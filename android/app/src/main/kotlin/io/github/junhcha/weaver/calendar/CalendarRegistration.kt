package io.github.junhcha.weaver.calendar

import io.github.junhcha.weaver.schema.Action
import io.github.junhcha.weaver.schema.ActionKind
import io.github.junhcha.weaver.schema.ActionResult
import io.github.junhcha.weaver.schema.CalendarPayload
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.io.File
import java.io.FileOutputStream
import java.time.Instant
import java.time.OffsetDateTime
import java.util.UUID

@Serializable
data class CalendarTarget(val id: Long, val name: String, val account: String, val type: String) {
    fun sameIdentity(other: CalendarTarget) = id == other.id && account == other.account && type == other.type
}
/**
 * One event this phone created. [threadId] (serialized as `ref`) is the registering Action's ID; journals
 * written by the earlier manual flow hold a Thread ID there and are never matched again.
 */
@Serializable
data class CalendarMapping(
    @SerialName("ref") val threadId: String,
    val calendar: CalendarTarget, val marker: String = "Weaver registration ${UUID.randomUUID()}",
    val uri: String? = null, val rowId: Long? = null,
    val event: CalendarEvent? = null,
    val manualReview: Boolean = false, val needsTarget: Boolean = false,
    val removed: Boolean = false,
)

enum class ActionPhase { PENDING, RETRY, DONE, FAILED }

/**
 * What this phone knows about one Action. DONE and FAILED are final: [finishedAt] is fixed when the
 * state becomes final so every report of it is byte-identical. [note] explains a wait that is not a failure
 * (`target`: no usable calendar, `permission`: calendar access missing).
 */
@Serializable
data class ActionState(
    val actionId: String, val kind: ActionKind, val title: String, val phase: ActionPhase = ActionPhase.PENDING,
    val attempts: Int = 0, val nextAttemptAt: Long? = null,
    val externalId: String? = null, val error: String? = null,
    val finishedAt: String? = null, val note: String? = null,
) {
    val isFinal get() = phase == ActionPhase.DONE || phase == ActionPhase.FAILED
}

@Serializable
private data class CalendarState(
    val selected: CalendarTarget? = null, val mappings: List<CalendarMapping> = emptyList(),
    val actions: List<ActionState> = emptyList(),
)

/** Backup-excluded local journal. A pending marker is fsynced BEFORE crossing the provider boundary.
 * All access is serialized by CalendarRuntime.lock, including recovery and repeated runs.
 */
class CalendarJournal(private val file: File) {
    // Journals written before actions had their own state carry extra keys.
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
    fun actions() = read().actions
    fun action(id: String) = read().actions.firstOrNull { it.actionId == id }
    fun put(action: ActionState) {
        val state = read()
        save(state.copy(actions = state.actions.filterNot { it.actionId == action.actionId } + action))
    }
}

@Serializable
data class CalendarEvent(val title: String, val start: Long, val end: Long, val timezone: String) {
    companion object {
        /** A schedule without an end lasts one hour. */
        fun from(payload: CalendarPayload): CalendarEvent {
            val start = OffsetDateTime.parse(payload.start).toInstant().toEpochMilli()
            val end = payload.end?.let { OffsetDateTime.parse(it).toInstant().toEpochMilli() } ?: (start + 3_600_000)
            return CalendarEvent(payload.title, start, end, payload.timezone)
        }
    }
}

data class CalendarRow(val uri: String, val id: Long, val calendarId: Long, val event: CalendarEvent?)

interface CalendarProvider {
    fun calendars(): List<CalendarTarget>
    /** An empty list proves absence; query failures MUST throw. Return ALL marker matches. */
    fun find(marker: String): List<CalendarRow>
    fun insert(target: CalendarTarget, marker: String, event: CalendarEvent): Pair<String, Long>
    /** False when the event no longer exists. Other failures throw. */
    fun update(uri: String, event: CalendarEvent): Boolean
    /** False when the event was already gone. Other failures throw. */
    fun delete(uri: String): Boolean
}

/**
 * Runs the PC's calendar Actions on the phone automatically. The pending marker and the recovery
 * rules of the earlier manual registration are kept: a register is journaled before the provider
 * is touched, an ambiguous find never inserts a second event, and a query failure never counts as
 * proof of absence. Update and remove set a known event to a known state, so repeating them is safe.
 *
 * A wait that is not a failure (no calendar chosen, permission missing) does not use up an attempt.
 */
class ActionExecutor(
    private val journal: CalendarJournal, private val provider: CalendarProvider,
    private val now: () -> Long = System::currentTimeMillis, private val maxAttempts: Int = 5,
) {
    private sealed interface Step {
        data class Done(val externalId: String) : Step
        data class Failed(val error: String) : Step
        data class Waiting(val note: String) : Step
    }

    /** Executes what is due and returns the final results to report, in sequence order. */
    fun run(actions: List<Action>): List<ActionResult> {
        val results = mutableListOf<ActionResult>()
        for (action in actions.sortedBy { it.seq }) {
            var state = journal.action(action.actionId)
                ?: ActionState(action.actionId, action.kind, action.payload.title).also { journal.put(it) }
            if (!state.isFinal) {
                if (state.nextAttemptAt?.let { it > now() } == true) continue
                state = execute(action, state)
            }
            if (state.isFinal) results += result(state)
        }
        return results
    }

    private fun execute(action: Action, state: ActionState): ActionState {
        val target = journal.selected() ?: return waiting(state, "target")
        val step = try {
            when (action.kind) {
                ActionKind.REGISTER -> register(action, target)
                ActionKind.UPDATE -> update(action)
                ActionKind.REMOVE -> remove(action)
            }
        } catch (_: SecurityException) {
            Step.Waiting("permission")
        } catch (error: Exception) {
            return retryOrGiveUp(state, error)
        }
        return when (step) {
            is Step.Waiting -> waiting(state, step.note)
            is Step.Done -> finish(state, ActionPhase.DONE, state.attempts + 1, externalId = step.externalId)
            is Step.Failed -> finish(state, ActionPhase.FAILED, state.attempts + 1, error = step.error)
        }
    }

    private fun waiting(state: ActionState, note: String) =
        state.copy(note = note, nextAttemptAt = null).also { journal.put(it) }

    private fun retryOrGiveUp(state: ActionState, error: Exception): ActionState {
        val attempts = state.attempts + 1
        if (attempts >= maxAttempts) return finish(state, ActionPhase.FAILED, attempts, error = "provider_error")
        val delay = minOf(60_000L shl (attempts - 1), 3_600_000L)
        return state.copy(phase = ActionPhase.RETRY, attempts = attempts, nextAttemptAt = now() + delay,
            error = errorCode(error), note = null).also { journal.put(it) }
    }

    private fun finish(state: ActionState, phase: ActionPhase, attempts: Int, externalId: String? = null, error: String? = null) =
        state.copy(phase = phase, attempts = attempts, nextAttemptAt = null, externalId = externalId, error = error,
            finishedAt = Instant.ofEpochMilli(now()).toString(), note = null).also { journal.put(it) }

    private fun errorCode(error: Exception) = error.javaClass.simpleName.lowercase().filter { it in 'a'..'z' }.ifEmpty { "provider_error" }

    private fun register(action: Action, target: CalendarTarget): Step {
        val old = journal.mappings().firstOrNull { it.threadId == action.actionId }
        if (old?.uri != null) return Step.Done(old.uri)
        val selected = provider.calendars().firstOrNull { it.sameIdentity(target) } ?: return Step.Waiting("target")
        val pending = old ?: CalendarMapping(action.actionId, selected, event = CalendarEvent.from(action.payload))
        journal.put(pending)
        // Never infer absence from query errors, or accept a copied DESCRIPTION as ownership.
        val rows = provider.find(pending.marker)
        val found = matching(pending, rows)
        if (found != null) {
            journal.put(pending.copy(uri = found.uri, rowId = found.id))
            return Step.Done(found.uri)
        }
        if (rows.isNotEmpty() || pending.manualReview || pending.event == null ||
            provider.calendars().none { it.sameIdentity(pending.calendar) }) return Step.Failed("manual_review")
        val row = provider.insert(pending.calendar, pending.marker, pending.event)
        journal.put(pending.copy(uri = row.first, rowId = row.second))
        return Step.Done(row.first)
    }

    private fun update(action: Action): Step {
        val uri = checkNotNull(action.externalId)
        val event = CalendarEvent.from(action.payload)
        if (!provider.update(uri, event)) return Step.Failed("event_missing")
        journal.mappings().firstOrNull { it.uri == uri }?.let { journal.put(it.copy(event = event)) }
        return Step.Done(uri)
    }

    private fun remove(action: Action): Step {
        val uri = checkNotNull(action.externalId)
        provider.delete(uri)
        journal.mappings().firstOrNull { it.uri == uri }?.let { journal.put(it.copy(removed = true)) }
        return Step.Done(uri)
    }

    private fun matching(pending: CalendarMapping, rows: List<CalendarRow>): CalendarRow? =
        rows.singleOrNull()?.takeIf { pending.event != null && it.calendarId == pending.calendar.id && it.event == pending.event }

    private fun result(state: ActionState) = ActionResult(
        io.github.junhcha.weaver.schema.REVIEW_SCHEMA_VERSION, state.actionId,
        if (state.phase == ActionPhase.DONE) ActionResult.Outcome.COMPLETED else ActionResult.Outcome.FAILED,
        state.externalId, state.attempts.toLong(), checkNotNull(state.finishedAt), state.error,
    )
}
