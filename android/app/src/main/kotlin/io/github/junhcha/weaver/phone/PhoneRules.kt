package io.github.junhcha.weaver.phone

import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId

/** The phone shows Korean calendar days regardless of the device zone. */
val SeoulZone: ZoneId = ZoneId.of("Asia/Seoul")

private val legacyHistoryKinds = setOf(ThreadKind.EXECUTION, ThreadKind.TAG_CORRECTION, ThreadKind.REJECTION)

/**
 * Fact/history split, same as web `isHistory` (web/src/lib/history.ts): the explicit presentation wins, otherwise
 * fall back to the legacy rules for older snapshots.
 */
fun PhoneThread.isHistory(): Boolean = when (presentation) {
    Presentation.HISTORY -> true
    Presentation.FACT -> false
    null -> kind in legacyHistoryKinds || amends != null || retracts != null || id.startsWith("wiki-field:") ||
        (id.startsWith("wiki:") && field != null && content == "$field 변경")
}

fun PhoneThread.historyTarget(): String? = correction?.target ?: retracts ?: executes ?: resolves ?: amends ?: updates

/** Web `relations()`: any retraction retracts; a resolve counts only if its row is neither a retraction nor retracted. */
private class Relations(threads: List<PhoneThread>) {
    val retracted: Set<String> = threads.mapNotNull { it.retracts }.toSet() +
        threads.filter { it.retracted == true }.map { it.id }
    val resolved: Set<String> = threads.mapNotNull { t ->
        t.resolves.takeIf { t.retracts == null && t.id !in retracted }
    }.toSet()

    fun isOpen(t: PhoneThread) = t.open ?: (t.kind == ThreadKind.REQUEST && t.id !in retracted && t.id !in resolved)
}

private fun PhoneSnapshot.partyNames(): Set<String> =
    tags.filter { it.kind?.isParty == true }.map { it.name }.toSet()

/** Newest first; seq breaks ties like the PC. */
internal val newestFirst = compareByDescending<PhoneThread> { it.occurredAt }.thenByDescending { it.seq }

private fun rootTarget(row: PhoneThread, byId: Map<String, PhoneThread>): PhoneThread {
    var current = row
    val seen = mutableSetOf(row.id)
    while (current.isHistory()) {
        val next = current.historyTarget()?.let(byId::get) ?: break
        if (!seen.add(next.id)) break
        current = next
    }
    return current
}

/**
 * Effective values like the web projection: history rows that `update` a fact's deadline/schedule are folded in
 * chronological order, tag corrections add/remove tags, and retracted changes are skipped. Returns every thread, with
 * [PhoneThread.deadline], [PhoneThread.scheduleAt] and [PhoneThread.tags] replaced on the facts they change.
 */
fun PhoneSnapshot.effective(): PhoneSnapshot {
    // The server already folded the history rows in (effective_deadline/_schedule, corrected tags): never apply them twice.
    if (threads.any { it.open != null }) return this
    val relation = Relations(threads)
    val byId = threads.associateBy { it.id }
    val changes = threads.filter { it.isHistory() && it.retracts == null && it.id !in relation.retracted }
        .sortedWith(compareBy<PhoneThread> { it.occurredAt }.thenBy { it.recordedAt }.thenBy { it.seq })
    val folded = threads.associateBy({ it.id }, { it }).toMutableMap()
    for (change in changes) {
        val root = rootTarget(change, byId).takeIf { !it.isHistory() } ?: continue
        var value = folded.getValue(root.id)
        if ((change.amends != null || change.updates != null) && change.field == "deadline") value = value.copy(deadline = change.deadline)
        if (change.updates != null && change.field == "schedule") value = value.copy(scheduleAt = change.scheduleAt)
        change.correction?.let { fix -> value = value.copy(tags = (value.tags + fix.add).distinct() - fix.remove.toSet()) }
        folded[root.id] = value
    }
    return copy(threads = threads.map { folded.getValue(it.id) })
}

/** Counts behind the 지금 status strip. */
data class StatusCounts(val waitingReview: Int, val processing: Int, val stopped: Int)

// ── 지금 ─────────────────────────────────────────────────────────────────────

enum class DueKind { DEADLINE, SCHEDULE }

/** [revised]: seq is at or before the acknowledged mark but a later revision brought it back into recent. */
data class NowEntry(val thread: PhoneThread, val dueKind: DueKind? = null, val revised: Boolean = false)

data class NowView(
    val overdue: List<NowEntry>,
    val today: List<NowEntry>,
    val recent: List<NowEntry>,
) {
    val isEmpty: Boolean get() = overdue.isEmpty() && today.isEmpty() && recent.isEmpty()
}

/**
 * Overdue: open requests (web `isOpen`) whose effective deadline day is before today. Today: open requests due today
 * and schedules on today's date. Recent: facts recorded since the start of yesterday that are not already listed above.
 * All day boundaries are in [zone] (Asia/Seoul).
 */
fun nowView(raw: PhoneSnapshot, now: Instant, zone: ZoneId = SeoulZone): NowView {
    val snapshot = raw.effective()
    val today = now.atZone(zone).toLocalDate()
    val relation = Relations(snapshot.threads)
    val facts = snapshot.threads.filter { !it.isHistory() && it.id !in relation.retracted }
    fun scheduleDay(t: PhoneThread): LocalDate? = t.scheduleAt?.atZone(zone)?.toLocalDate()

    val openRequests = facts.filter(relation::isOpen)
    val overdue = openRequests.filter { it.deadline?.isBefore(today) == true }
        .sortedWith(compareBy<PhoneThread> { it.deadline }.thenBy { it.seq })
        .map { NowEntry(it, DueKind.DEADLINE) }
    val dueToday = (openRequests.filter { it.deadline == today }.map { NowEntry(it, DueKind.DEADLINE) } +
        facts.filter { it.kind == ThreadKind.SCHEDULE && scheduleDay(it) == today }.map { NowEntry(it, DueKind.SCHEDULE) })
        .sortedBy { it.thread.scheduleAt ?: Instant.MAX }
    val listed = (overdue + dueToday).map { it.thread.id }.toSet()
    val since = today.minusDays(1).atStartOfDay(zone).toInstant()
    val recent = facts.filter { it.id !in listed && !it.recordedAt.isBefore(since) }
        .sortedWith(newestFirst)
        .map { NowEntry(it) }
    return NowView(overdue, dueToday, recent)
}

// ── 사람·영역 타임라인 ────────────────────────────────────────────────────────

enum class Membership { COUNTERPART, MENTION, AREA }

data class TimelineRow(val thread: PhoneThread, val membership: Membership, val isHistory: Boolean)

/**
 * Web `mentionMatch` for one subject: the subject appears in the tags but is not the counterpart. Only people and
 * organizations can be mention-only.
 */
fun mentionOnly(thread: PhoneThread, subject: String, parties: Set<String>): Boolean =
    subject in parties && thread.counterpart != subject &&
        (thread.mentions?.let { subject in it } ?: (subject in thread.tags))

/**
 * One person/organization/area as a single newest-first column. People default to threads where they are the
 * counterpart; [includeMentions] adds mention-only threads (marked `MENTION`). Areas match by tag. History rows are
 * hidden unless [includeHistory]; then each one follows its target, oldest first, like web `adjacentHistory`.
 * A history row belongs to the subject through its own tags or through the fact it changes.
 */
fun timeline(
    raw: PhoneSnapshot,
    subject: String,
    includeMentions: Boolean = false,
    includeHistory: Boolean = false,
): List<TimelineRow> {
    val snapshot = raw.effective()
    val parties = snapshot.partyNames()
    val isParty = subject in parties
    val byId = snapshot.threads.associateBy { it.id }
    fun membership(t: PhoneThread): Membership? = when {
        isParty && t.counterpart == subject -> Membership.COUNTERPART
        isParty && mentionOnly(t, subject, parties) -> if (includeMentions) Membership.MENTION else null
        !isParty && subject in t.tags -> Membership.AREA
        else -> null
    }
    val rows = snapshot.threads.mapNotNull { t ->
        val own = membership(t)
        val viaTarget = if (own == null && t.isHistory()) membership(rootTarget(t, byId)) else null
        (own ?: viaTarget)?.let { TimelineRow(t, it, t.isHistory()) }
    }.filter { includeHistory || !it.isHistory }
    if (!includeHistory) return rows.sortedWith(compareBy(newestFirst) { it.thread })

    val ids = rows.map { it.thread.id }.toSet()
    val children = rows.filter { it.isHistory && it.thread.historyTarget() in ids }
        .groupBy { it.thread.historyTarget() }
    val roots = rows.filter { !(it.isHistory && it.thread.historyTarget() in ids) }
        .sortedWith(compareBy(newestFirst) { it.thread })
    val seen = mutableSetOf<String>()
    val result = mutableListOf<TimelineRow>()
    fun emit(row: TimelineRow) {
        if (!seen.add(row.thread.id)) return
        result += row
        children[row.thread.id].orEmpty()
            .sortedWith(compareBy<TimelineRow> { it.thread.recordedAt }.thenBy { it.thread.seq })
            .forEach(::emit)
    }
    roots.forEach(::emit)
    rows.forEach(::emit) // history cycles never reach a root
    return result
}

/** People/organizations that have threads as counterpart or mention, busiest first. */
fun partyDirectory(raw: PhoneSnapshot): List<Pair<PhoneTag, Int>> {
    val snapshot = raw.effective()
    val facts = snapshot.threads.filter { !it.isHistory() }
    return snapshot.tags.filter { it.kind?.isParty == true }
        .map { tag -> tag to facts.count { it.counterpart == tag.name || tag.name in it.tags } }
        .sortedWith(compareByDescending<Pair<PhoneTag, Int>> { it.second }.thenBy { it.first.name })
}

fun areaDirectory(raw: PhoneSnapshot): List<Pair<PhoneTag, Int>> {
    val snapshot = raw.effective()
    val facts = snapshot.threads.filter { !it.isHistory() }
    return snapshot.tags.filter { it.kind == TagKind.AREA }
        .map { tag -> tag to facts.count { tag.name in it.tags } }
        .sortedWith(compareByDescending<Pair<PhoneTag, Int>> { it.second }.thenBy { it.first.name })
}

/** Where a Wiki link leads. */
sealed interface LinkTarget {
    data class Person(val name: String) : LinkTarget
    data class Area(val name: String) : LinkTarget
    data class Page(val id: String) : LinkTarget
    /** A compiled result document (server feature `results`); [bulletId] is the bullet or block to scroll to. */
    data class Document(val id: String, val bulletId: String? = null) : LinkTarget
    data object None : LinkTarget
}

/**
 * [documents] are the server's result documents (empty while the server has no `results`): a link whose name is a
 * document's title opens that document, before the area timeline or the temporary Wiki page.
 */
fun resolveLink(
    snapshot: PhoneSnapshot, name: String,
    documents: List<io.github.junhcha.weaver.schema.DocumentSummary> = emptyList(),
): LinkTarget {
    val kind = snapshot.tags.firstOrNull { it.name == name }?.kind
    val document = documents.firstOrNull { it.title == name && it.kind != io.github.junhcha.weaver.schema.DocumentKind.DIGEST }
    return when {
        kind?.isParty == true -> LinkTarget.Person(name)
        document != null -> LinkTarget.Document(document.documentId)
        kind == TagKind.AREA -> LinkTarget.Area(name)
        else -> snapshot.wikiPages.firstOrNull { it.title == name }?.let { LinkTarget.Page(it.id) } ?: LinkTarget.None
    }
}
