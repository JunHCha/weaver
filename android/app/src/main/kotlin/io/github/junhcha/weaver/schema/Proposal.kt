package io.github.junhcha.weaver.schema

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

/**
 * One source review unit from proposals/<proposal_id>.r<rev>.json. Mirrors
 * schema/Proposal.schema.json. Cross-reference integrity (unique/dangling refs, block
 * propagation) is enforced by the server and rechecked by ReviewDraft before approval/edit.
 * Inbox decoding keeps the fields as given.
 *
 * A non-null `blockedByRequest` forbids calendar registration until a human decision Entry
 * resolves the request. Approving the proposal does not lift it.
 */
@Serializable
data class Proposal(
    @SerialName("schema_version") val schemaVersion: Int,
    @SerialName("proposal_id") val proposalId: String,
    val rev: Long,
    val source: SourceSummary,
    val summary: String,
    @SerialName("thread_excerpt") val threadExcerpt: String,
    val entities: List<ProposalEntity>,
    val schedules: List<Schedule>,
    val relations: List<RelationData>,
    @SerialName("entry_drafts") val entryDrafts: List<EntryDraft>,
) {
    init {
        Rules.schemaVersion(schemaVersion)
        Rules.prefixedId("prp", proposalId, "proposal_id")
        Rules.revision(rev)
        Rules.nonEmpty(summary, "summary")
        Rules.nonEmpty(threadExcerpt, "thread_excerpt")
    }

    @Serializable
    data class SourceSummary(
        @SerialName("source_id") val sourceId: String,
        val channel: Channel,
        @SerialName("captured_at") val capturedAt: String,
        @SerialName("occurred_at") val occurredAt: String?,
    ) {
        init {
            Rules.prefixedId("src", sourceId, "source.source_id")
            Rules.timestamp(capturedAt, "source.captured_at")
            occurredAt?.let { Rules.timestamp(it, "source.occurred_at") }
        }
    }

    /** Human confirmation question. Choices may be empty; free-text answers are allowed. */
    @Serializable
    data class Ask(val question: String, val choices: List<Choice>) {
        init {
            Rules.nonEmpty(question, "ask.question")
        }
    }

    @Serializable
    data class Choice(@SerialName("entity_id") val entityId: String, val label: String) {
        init {
            Rules.prefixedId("ent", entityId, "choice.entity_id")
            Rules.nonEmpty(label, "choice.label")
        }
    }

    @Serializable
    enum class EntityType {
        @SerialName("person") PERSON,
        @SerialName("org") ORG,
        @SerialName("project") PROJECT,
        @SerialName("topic") TOPIC,
        @SerialName("meeting") MEETING,
    }

    @Serializable
    data class ProposalEntity(
        val confidence: Double,
        val ask: Ask?,
        @SerialName("thread_excerpt") val threadExcerpt: String,
        val ref: String,
        @SerialName("entity_id") val entityId: String?,
        val type: EntityType,
        val name: String,
        val aliases: List<String>,
    ) {
        init {
            Rules.confidence(confidence)
            Rules.nonEmpty(threadExcerpt, "entity.thread_excerpt")
            Rules.ref(ref, "entity.ref")
            entityId?.let { Rules.prefixedId("ent", it, "entity.entity_id") }
            Rules.nonEmpty(name, "entity.name")
            aliases.forEach { Rules.nonEmpty(it, "entity.aliases[]") }
        }
    }

    @Serializable
    data class Schedule(
        val confidence: Double,
        val ask: Ask?,
        @SerialName("thread_excerpt") val threadExcerpt: String,
        val ref: String,
        val title: String,
        val start: String,
        val end: String?,
        val entities: List<String>,
        @SerialName("entry_ref") val entryRef: String,
        @SerialName("blocked_by_request") val blockedByRequest: String?,
    ) {
        init {
            Rules.confidence(confidence)
            Rules.nonEmpty(threadExcerpt, "schedule.thread_excerpt")
            Rules.ref(ref, "schedule.ref")
            Rules.nonEmpty(title, "schedule.title")
            Rules.timestamp(start, "schedule.start")
            end?.let { Rules.timestamp(it, "schedule.end") }
            entities.forEach { Rules.entityRef(it, "schedule.entities[]") }
            Rules.ref(entryRef, "schedule.entry_ref")
            blockedByRequest?.let { Rules.ref(it, "schedule.blocked_by_request") }
        }
    }

    @Serializable
    enum class RelationType {
        @SerialName("원인") CAUSE,
        @SerialName("후속조치") FOLLOW_UP,
        @SerialName("같은논의") SAME_DISCUSSION,
        @SerialName("참석") ATTENDS,
        @SerialName("소속") MEMBER_OF,
    }

    /** `from`/`to` are entity refs, ent_ IDs, or `this` for the proposal source. */
    @Serializable
    data class RelationData(
        val confidence: Double,
        val ask: Ask?,
        @SerialName("thread_excerpt") val threadExcerpt: String,
        val from: String,
        val type: RelationType,
        val to: String,
        val reason: String?,
    ) {
        init {
            Rules.confidence(confidence)
            Rules.nonEmpty(threadExcerpt, "relation.thread_excerpt")
            if (from != THIS) Rules.entityRef(from, "relation.from")
            if (to != THIS) Rules.entityRef(to, "relation.to")
            reason?.let { Rules.nonEmpty(it, "relation.reason") }
        }

        companion object {
            const val THIS = "this"
        }
    }

    @Serializable
    enum class EntryKind {
        @SerialName("요청") REQUEST,
        @SerialName("논의") DISCUSSION,
        @SerialName("결정") DECISION,
        @SerialName("폐기") DISCARD,
        @SerialName("약속") PROMISE,
        @SerialName("일정") SCHEDULE,
        @SerialName("변경") CHANGE,
    }

    /** A proposed fact, never an execution command. `resolves` is always null in v1. */
    @Serializable
    data class EntryDraft(
        val confidence: Double,
        val ask: Ask?,
        @SerialName("thread_excerpt") val threadExcerpt: String,
        val ref: String,
        val entities: List<String>,
        val kind: EntryKind,
        val field: String?,
        val value: JsonElement,
        @SerialName("occurred_at") val occurredAt: String,
        val supersedes: String?,
        val retracts: String?,
        val request: Request?,
        val resolves: Nothing?,
        @SerialName("blocked_by_request") val blockedByRequest: String?,
    ) {
        init {
            Rules.confidence(confidence)
            Rules.nonEmpty(threadExcerpt, "entry_draft.thread_excerpt")
            Rules.ref(ref, "entry_draft.ref")
            entities.forEach { Rules.entityRef(it, "entry_draft.entities[]") }
            field?.let { Rules.nonEmpty(it, "entry_draft.field") }
            Rules.timestamp(occurredAt, "entry_draft.occurred_at")
            supersedes?.let { Rules.prefixedId("evt", it, "entry_draft.supersedes") }
            retracts?.let { Rules.prefixedId("evt", it, "entry_draft.retracts") }
            require((kind == EntryKind.REQUEST) == (request != null)) {
                "request must be non-null exactly when kind is 요청"
            }
            blockedByRequest?.let { Rules.ref(it, "entry_draft.blocked_by_request") }
        }
    }

    /** Data awaiting a user decision, never execution permission. */
    @Serializable
    data class Request(
        val requester: String,
        val targets: List<String>,
        @SerialName("due_at") val dueAt: String?,
        val content: String,
    ) {
        init {
            Rules.entityRef(requester, "request.requester")
            require(targets.isNotEmpty()) { "request.targets must not be empty" }
            targets.forEach { Rules.entityRef(it, "request.targets[]") }
            dueAt?.let { Rules.timestamp(it, "request.due_at") }
            Rules.nonEmpty(content, "request.content")
        }
    }
}
