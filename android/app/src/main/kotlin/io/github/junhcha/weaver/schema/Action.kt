package io.github.junhcha.weaver.schema

import java.time.OffsetDateTime
import java.time.ZoneId
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** The calendar event an [Action] creates, changes to, or removes. Mirrors `$defs/CalendarPayload`. */
@Serializable
data class CalendarPayload(
    val start: String,
    val end: String?,
    val timezone: String,
    val title: String,
) {
    init {
        Rules.timestamp(start, "start")
        end?.let { Rules.timestamp(it, "end"); require(OffsetDateTime.parse(it).toInstant() > OffsetDateTime.parse(start).toInstant()) }
        ZoneId.of(timezone)
        Rules.nonEmpty(title, "title")
    }
}

@Serializable
enum class ActionKind {
    @SerialName("calendar.register") REGISTER,
    @SerialName("calendar.update") UPDATE,
    @SerialName("calendar.remove") REMOVE,
}

/**
 * PC → `actions/<action_id>.json`. Mirrors schema/Action.schema.json. [externalId] names the calendar
 * event the phone created earlier and is present exactly for update and remove.
 */
@Serializable
data class Action(
    @SerialName("schema_version") val schemaVersion: Int,
    @SerialName("action_id") val actionId: String,
    val kind: ActionKind,
    @SerialName("thread_id") val threadId: String,
    val seq: Long,
    @SerialName("input_version") val inputVersion: Long,
    val payload: CalendarPayload,
    @SerialName("external_id") val externalId: String?,
) {
    init {
        Rules.schemaVersion(schemaVersion, REVIEW_SCHEMA_VERSION)
        Rules.actionId(actionId)
        Rules.nonEmpty(threadId, "thread_id")
        Rules.revision(seq)
        Rules.revision(inputVersion)
        externalId?.let { Rules.nonEmpty(it, "external_id") }
        require((externalId == null) == (kind == ActionKind.REGISTER)) { "external_id is required exactly for update and remove" }
    }
}

/** Phone → `action-results/<action_id>.json`. Mirrors schema/ActionResult.schema.json. */
@Serializable
data class ActionResult(
    @SerialName("schema_version") val schemaVersion: Int,
    @SerialName("action_id") val actionId: String,
    val outcome: Outcome,
    @SerialName("external_id") val externalId: String?,
    val attempts: Long,
    @SerialName("finished_at") val finishedAt: String,
    /** A short `[a-z_]+` code, never event content. */
    val error: String?,
) {
    init {
        Rules.schemaVersion(schemaVersion, REVIEW_SCHEMA_VERSION)
        Rules.actionId(actionId)
        externalId?.let { Rules.nonEmpty(it, "external_id") }
        Rules.revision(attempts)
        Rules.timestamp(finishedAt, "finished_at")
        error?.let { Rules.nonEmpty(it, "error") }
        when (outcome) {
            Outcome.COMPLETED -> require(externalId != null && error == null) { "a completed action needs external_id and no error" }
            Outcome.FAILED -> require(externalId == null && error != null) { "a failed action needs an error and no external_id" }
        }
    }

    @Serializable
    enum class Outcome {
        @SerialName("completed") COMPLETED, @SerialName("failed") FAILED,
    }
}
