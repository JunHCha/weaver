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
    null -> kind in legacyHistoryKinds || retracts != null || id.startsWith("wiki-field:") ||
        (id.startsWith("wiki:") && field != null && content == "$field 변경")
}

fun PhoneThread.historyTarget(): String? = correctionTarget ?: retracts ?: executes ?: resolves ?: updates

/** Ids of facts a later history row retracted or resolved; they no longer count as open work. */
private fun PhoneSnapshot.closedIds(): Set<String> =
    threads.filter { it.isHistory() }.flatMap { listOfNotNull(it.retracts, it.resolves) }.toSet()

private fun PhoneSnapshot.partyNames(): Set<String> =
    tags.filter { it.kind?.isParty == true }.map { it.name }.toSet()

/** Newest first; seq breaks ties like the PC. */
private val newestFirst = compareByDescending<PhoneThread> { it.occurredAt }.thenByDescending { it.seq }

// ── 지금 ─────────────────────────────────────────────────────────────────────

enum class DueKind { DEADLINE, SCHEDULE }

data class NowEntry(val thread: PhoneThread, val dueKind: DueKind? = null)

data class NowView(
    val overdue: List<NowEntry>,
    val today: List<NowEntry>,
    val recent: List<NowEntry>,
) {
    val isEmpty: Boolean get() = overdue.isEmpty() && today.isEmpty() && recent.isEmpty()
}

/**
 * Overdue: open requests whose deadline day is before today. Today: deadlines and schedules that fall on today's
 * date. Recent: facts recorded since the start of yesterday that are not already listed above. All day boundaries
 * are in [zone] (Asia/Seoul).
 */
fun nowView(snapshot: PhoneSnapshot, now: Instant, zone: ZoneId = SeoulZone): NowView {
    val today = now.atZone(zone).toLocalDate()
    val closed = snapshot.closedIds()
    val facts = snapshot.threads.filter { !it.isHistory() && it.id !in closed }
    fun scheduleDay(t: PhoneThread): LocalDate? = t.scheduleAt?.atZone(zone)?.toLocalDate()

    val overdue = facts.filter { it.deadline?.isBefore(today) == true }
        .sortedWith(compareBy<PhoneThread> { it.deadline }.thenBy { it.seq })
        .map { NowEntry(it, DueKind.DEADLINE) }
    val dueToday = facts.mapNotNull { t ->
        when {
            t.deadline == today -> NowEntry(t, DueKind.DEADLINE) to (t.scheduleAt ?: Instant.MAX)
            scheduleDay(t) == today -> NowEntry(t, DueKind.SCHEDULE) to checkNotNull(t.scheduleAt)
            else -> null
        }
    }.sortedBy { it.second }.map { it.first }
    val listed = (overdue + dueToday).map { it.thread.id }.toSet()
    val since = today.minusDays(1).atStartOfDay(zone).toInstant()
    val recent = facts.filter { it.id !in listed && !it.recordedAt.isBefore(since) }
        .sortedWith(compareByDescending<PhoneThread> { it.recordedAt }.thenByDescending { it.seq })
        .map { NowEntry(it) }
    return NowView(overdue, dueToday, recent)
}

// ── 파이프라인 상태 줄 ────────────────────────────────────────────────────────

data class StatusCounts(val waitingReview: Int, val processing: Int, val stopped: Int)

/** Status values of pipeline items, as `MapItem.status` carries them. */
fun statusCounts(statuses: List<String>): StatusCounts = StatusCounts(
    waitingReview = statuses.count { it == "review" },
    stopped = statuses.count { it == "apply_stopped" },
    processing = statuses.count { it !in setOf("review", "apply_stopped", "done", "rejected") },
)

// ── 사람·영역 타임라인 ────────────────────────────────────────────────────────

enum class Membership { COUNTERPART, MENTION, AREA }

data class TimelineRow(val thread: PhoneThread, val membership: Membership, val isHistory: Boolean)

/**
 * Web `mentionMatch` for one subject: the subject appears in the tags but is not the counterpart. Only people and
 * organizations can be mention-only.
 */
fun mentionOnly(thread: PhoneThread, subject: String, parties: Set<String>): Boolean =
    subject in parties && thread.counterpart != subject && subject in thread.tags

private fun PhoneSnapshot.rootTarget(row: PhoneThread, byId: Map<String, PhoneThread>): PhoneThread {
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
 * One person/organization/area as a single newest-first column. People default to threads where they are the
 * counterpart; [includeMentions] adds mention-only threads (marked `MENTION`). Areas match by tag. History rows are
 * hidden unless [includeHistory]; then each one follows its target, oldest first, like web `adjacentHistory`.
 * A history row belongs to the subject through its own tags or through the fact it changes.
 */
fun timeline(
    snapshot: PhoneSnapshot,
    subject: String,
    includeMentions: Boolean = false,
    includeHistory: Boolean = false,
): List<TimelineRow> {
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
        val viaTarget = if (own == null && t.isHistory()) membership(snapshot.rootTarget(t, byId)) else null
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
fun partyDirectory(snapshot: PhoneSnapshot): List<Pair<PhoneTag, Int>> {
    val facts = snapshot.threads.filter { !it.isHistory() }
    return snapshot.tags.filter { it.kind?.isParty == true }
        .map { tag -> tag to facts.count { it.counterpart == tag.name || tag.name in it.tags } }
        .sortedWith(compareByDescending<Pair<PhoneTag, Int>> { it.second }.thenBy { it.first.name })
}

fun areaDirectory(snapshot: PhoneSnapshot): List<Pair<PhoneTag, Int>> {
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
    data object None : LinkTarget
}

fun resolveLink(snapshot: PhoneSnapshot, name: String): LinkTarget {
    val kind = snapshot.tags.firstOrNull { it.name == name }?.kind
    return when {
        kind?.isParty == true -> LinkTarget.Person(name)
        kind == TagKind.AREA -> LinkTarget.Area(name)
        else -> snapshot.wikiPages.firstOrNull { it.title == name }?.let { LinkTarget.Page(it.id) } ?: LinkTarget.None
    }
}
