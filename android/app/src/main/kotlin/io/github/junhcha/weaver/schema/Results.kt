package io.github.junhcha.weaver.schema

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
enum class LinkKind {
    @SerialName("document") DOCUMENT,
    @SerialName("digest") DIGEST,
}

@Serializable
enum class DueKind {
    @SerialName("deadline") DEADLINE,
    @SerialName("schedule") SCHEDULE,
}

/** Where a result opens: a compiled document bullet, or the day's Digest. */
@Serializable
data class LinkTarget(
    val kind: LinkKind,
    @SerialName("document_id") val documentId: String,
    @SerialName("bullet_id") val bulletId: String?,
    @SerialName("block_id") val blockId: String?,
) {
    init {
        Rules.nonEmpty(documentId, "document_id")
        bulletId?.let { Rules.nonEmpty(it, "bullet_id") }
        blockId?.let { Rules.nonEmpty(it, "block_id") }
    }
}

/** One card of `GET /read/cards`. Unversioned: the read API carries no schema_version. */
@Serializable
data class ResultCard(
    @SerialName("card_id") val cardId: String,
    val day: String,
    val key: String,
    val sphere: String,
    val headline: String,
    val bullets: List<String>,
    val people: Long,
    val sources: Long,
    val counterparts: List<String>,
    val channels: List<String>,
    val due: String?,
    @SerialName("due_label") val dueLabel: String?,
    @SerialName("due_kind") val dueKind: DueKind?,
    @SerialName("thread_ids") val threadIds: List<String>,
    val link: LinkTarget?,
    val reflecting: Boolean,
) {
    init {
        Rules.nonEmpty(cardId, "card_id")
        Rules.date(day, "day")
        Rules.nonEmpty(key, "key")
        Rules.nonEmpty(sphere, "sphere")
        Rules.nonEmpty(headline, "headline")
        require(bullets.size <= 2) { "bullets holds at most 2" }
        bullets.forEach { Rules.nonEmpty(it, "bullets") }
        Rules.offset(people, "people")
        Rules.offset(sources, "sources")
        require(counterparts.size <= 3) { "counterparts holds at most 3" }
        counterparts.forEach { Rules.nonEmpty(it, "counterparts") }
        channels.forEach { Rules.nonEmpty(it, "channels") }
        due?.let { Rules.date(it, "due") }
        threadIds.forEach { Rules.nonEmpty(it, "thread_ids") }
    }
}

/** One open received request of `GET /read/tasks`. */
@Serializable
data class TaskItem(
    @SerialName("thread_id") val threadId: String,
    val revision: Long,
    val content: String,
    @SerialName("block_id") val blockId: String,
    val deadline: String?,
    @SerialName("due_label") val dueLabel: String?,
    val counterpart: String?,
    @SerialName("document_id") val documentId: String?,
    @SerialName("document_title") val documentTitle: String?,
    val link: LinkTarget?,
) {
    init {
        Rules.nonEmpty(threadId, "thread_id")
        Rules.revision(revision)
        Rules.nonEmpty(content, "content")
        Rules.nonEmpty(blockId, "block_id")
        deadline?.let { Rules.date(it, "deadline") }
        counterpart?.let { Rules.nonEmpty(it, "counterpart") }
        documentId?.let { Rules.nonEmpty(it, "document_id") }
        documentTitle?.let { Rules.nonEmpty(it, "document_title") }
    }
}

@Serializable
enum class TreeNodeKind {
    @SerialName("area") AREA,
    @SerialName("project") PROJECT,
    @SerialName("topic") TOPIC,
}

/** One node of `GET /read/tree`: a document or card node, or an ancestor of one. */
@Serializable
data class TreeNodeView(
    val path: String,
    val label: String,
    val kind: TreeNodeKind,
    val parent: String?,
    @SerialName("document_id") val documentId: String?,
    @SerialName("open_tasks") val openTasks: Long,
    @SerialName("cards_7d") val cards7d: Long,
) {
    init {
        Rules.nonEmpty(path, "path")
        Rules.nonEmpty(label, "label")
        parent?.let { Rules.nonEmpty(it, "parent") }
        documentId?.let { Rules.nonEmpty(it, "document_id") }
        Rules.offset(openTasks, "open_tasks")
        Rules.offset(cards7d, "cards_7d")
    }
}

/** A reference note or vault heading related to a document. */
@Serializable
data class RefLinkView(
    val sphere: String,
    val path: String,
    val heading: String,
    val home: Boolean,
) {
    init {
        Rules.nonEmpty(sphere, "sphere")
        Rules.nonEmpty(path, "path")
        Rules.nonEmpty(heading, "heading")
    }
}

/** One line of a document section; [task] is set for a checkbox line. */
@Serializable
data class DocBullet(
    @SerialName("bullet_id") val bulletId: String,
    val text: String,
    @SerialName("thread_ids") val threadIds: List<String>,
    val task: TaskItem?,
    val day: String?,
    val label: String?,
) {
    init {
        Rules.nonEmpty(bulletId, "bullet_id")
        Rules.nonEmpty(text, "text")
        threadIds.forEach { Rules.nonEmpty(it, "thread_ids") }
        day?.let { Rules.date(it, "day") }
        label?.let { Rules.nonEmpty(it, "label") }
    }
}

@Serializable
enum class DocSectionKind {
    @SerialName("current") CURRENT,
    @SerialName("remaining") REMAINING,
    @SerialName("waiting") WAITING,
    @SerialName("cautions") CAUTIONS,
    @SerialName("tasks") TASKS,
    @SerialName("summary") SUMMARY,
}

@Serializable
data class DocSection(
    val kind: DocSectionKind,
    val title: String,
    val bullets: List<DocBullet>,
) {
    init {
        Rules.nonEmpty(title, "title")
    }
}

@Serializable
enum class DocumentKind {
    @SerialName("area") AREA,
    @SerialName("project") PROJECT,
    @SerialName("topic") TOPIC,
    @SerialName("digest") DIGEST,
}

/** One row of `GET /read/documents`: a [DocumentView] without sections. */
@Serializable
data class DocumentSummary(
    @SerialName("document_id") val documentId: String,
    val kind: DocumentKind,
    val key: String?,
    val title: String,
    @SerialName("as_of") val asOf: String?,
    val sha256: String,
) {
    init {
        Rules.nonEmpty(documentId, "document_id")
        key?.let { Rules.nonEmpty(it, "key") }
        Rules.nonEmpty(title, "title")
        asOf?.let { Rules.date(it, "as_of") }
        Rules.digest(sha256, "sha256")
    }
}

/** A compiled document or Digest (`GET /read/documents/{id}`); remaining and waiting are live. */
@Serializable
data class DocumentView(
    @SerialName("document_id") val documentId: String,
    val kind: DocumentKind,
    val key: String?,
    val title: String,
    @SerialName("as_of") val asOf: String?,
    val path: String,
    val sections: List<DocSection>,
    val references: List<RefLinkView>,
    @SerialName("log_subject") val logSubject: String?,
    val reflecting: Boolean,
    val sha256: String,
) {
    init {
        Rules.nonEmpty(documentId, "document_id")
        key?.let { Rules.nonEmpty(it, "key") }
        Rules.nonEmpty(title, "title")
        asOf?.let { Rules.date(it, "as_of") }
        Rules.nonEmpty(path, "path")
        logSubject?.let { Rules.nonEmpty(it, "log_subject") }
        Rules.digest(sha256, "sha256")
    }
}

@Serializable
data class EvidenceThread(
    @SerialName("thread_id") val threadId: String,
    val kind: String,
    val content: String?,
    @SerialName("occurred_at") val occurredAt: String,
    @SerialName("source_id") val sourceId: String,
    @SerialName("source_label") val sourceLabel: String,
) {
    init {
        Rules.nonEmpty(threadId, "thread_id")
        Rules.nonEmpty(kind, "kind")
        Rules.timestamp(occurredAt, "occurred_at")
        Rules.prefixedId("src", sourceId, "source_id")
        Rules.nonEmpty(sourceLabel, "source_label")
    }
}

/** The whole source text, cut at 20000 characters ([truncated]); no highlight in phase 1. */
@Serializable
data class EvidenceSource(
    @SerialName("source_id") val sourceId: String,
    val label: String,
    val channel: String?,
    @SerialName("occurred_at") val occurredAt: String?,
    val text: String,
    val truncated: Boolean,
) {
    init {
        Rules.prefixedId("src", sourceId, "source_id")
        Rules.nonEmpty(label, "label")
        channel?.let { Rules.nonEmpty(it, "channel") }
        occurredAt?.let { Rules.timestamp(it, "occurred_at") }
    }
}

/** `GET /read/documents/{id}/bullets/{bullet_id}/evidence`. */
@Serializable
data class Evidence(
    @SerialName("document_id") val documentId: String,
    @SerialName("bullet_id") val bulletId: String,
    val text: String,
    val threads: List<EvidenceThread>,
    val sources: List<EvidenceSource>,
) {
    init {
        Rules.nonEmpty(documentId, "document_id")
        Rules.nonEmpty(bulletId, "bullet_id")
        Rules.nonEmpty(text, "text")
    }
}

@Serializable
enum class UsedInKind {
    @SerialName("document") DOCUMENT,
    @SerialName("digest") DIGEST,
}

@Serializable
data class UsedIn(
    val kind: UsedInKind,
    @SerialName("document_id") val documentId: String,
    val title: String,
    @SerialName("bullet_id") val bulletId: String?,
    @SerialName("block_id") val blockId: String?,
) {
    init {
        Rules.nonEmpty(documentId, "document_id")
        Rules.nonEmpty(title, "title")
        bulletId?.let { Rules.nonEmpty(it, "bullet_id") }
        blockId?.let { Rules.nonEmpty(it, "block_id") }
    }
}

/** One row of `GET /read/sources`: a source, its Threads and where they are used. */
@Serializable
data class SourceLogEntry(
    @SerialName("source_id") val sourceId: String,
    val label: String,
    val channel: String?,
    @SerialName("occurred_at") val occurredAt: String,
    val spheres: List<String>,
    val paths: List<String>,
    val people: List<String>,
    val threads: List<EvidenceThread>,
    @SerialName("used_in") val usedIn: List<UsedIn>,
) {
    init {
        Rules.prefixedId("src", sourceId, "source_id")
        Rules.nonEmpty(label, "label")
        channel?.let { Rules.nonEmpty(it, "channel") }
        Rules.timestamp(occurredAt, "occurred_at")
        spheres.forEach { Rules.nonEmpty(it, "spheres") }
        paths.forEach { Rules.nonEmpty(it, "paths") }
        people.forEach { Rules.nonEmpty(it, "people") }
    }
}

@Serializable
enum class FeedbackChip {
    @SerialName("사실이 틀림") WRONG_FACT,
    @SerialName("이미 끝남") ALREADY_DONE,
    @SerialName("다른 건이 섞임") MIXED_CASES,
    @SerialName("제목이 이상함") ODD_TITLE,
}

/** The chip's wire text, which is also what the phone shows. */
val FeedbackChip.label: String get() = FeedbackChip.serializer().descriptor.getElementName(ordinal)

@Serializable
enum class FeedbackStatus {
    @SerialName("pending") PENDING,
    @SerialName("applied") APPLIED,
    @SerialName("failed") FAILED,
    @SerialName("undone") UNDONE,
}

@Serializable
enum class FeedbackClassification {
    @SerialName("fact") FACT,
    @SerialName("structure") STRUCTURE,
    @SerialName("both") BOTH,
    @SerialName("none") NONE,
}

/** A person's note on a document or bullet (`GET /read/documents/{id}/feedback`). */
@Serializable
data class FeedbackItem(
    @SerialName("feedback_id") val feedbackId: String,
    @SerialName("document_id") val documentId: String,
    @SerialName("bullet_id") val bulletId: String?,
    val text: String,
    val chips: List<FeedbackChip>,
    val status: FeedbackStatus,
    val classification: FeedbackClassification?,
    val result: String?,
    @SerialName("thread_ids") val threadIds: List<String>,
    @SerialName("created_at") val createdAt: String,
    @SerialName("updated_at") val updatedAt: String,
) {
    init {
        require(Regex("^fbk_[0-9a-f]{32}$").matches(feedbackId)) { "feedback_id must be fbk_ + 32 lowercase hex characters" }
        Rules.nonEmpty(documentId, "document_id")
        bulletId?.let { Rules.nonEmpty(it, "bullet_id") }
        Rules.nonEmpty(text, "text")
        require(text.length <= 2000) { "text must be at most 2000 characters" }
        threadIds.forEach { Rules.nonEmpty(it, "thread_ids") }
        Rules.timestamp(createdAt, "created_at")
        Rules.timestamp(updatedAt, "updated_at")
    }
}
