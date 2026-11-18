package io.github.junhcha.weaver.phone

import java.time.Clock
import java.time.Instant
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.ZoneId
import kotlinx.coroutines.runBlocking
import io.github.junhcha.weaver.pipeline.MapItem
import io.github.junhcha.weaver.pipeline.MapNode
import io.github.junhcha.weaver.ui.statusCounts
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PhoneRulesTest {
    private fun kst(date: String, time: String): Instant = OffsetDateTime.parse("${date}T$time:00+09:00").toInstant()

    private var next = 0L
    private fun thread(
        id: String,
        kind: ThreadKind = ThreadKind.RECORD,
        counterpart: String? = null,
        tags: List<String> = emptyList(),
        at: Instant = kst("2026-11-10", "10:00"),
    ) = PhoneThread(id, ++next, kind, id, at, at, "통화", counterpart, tags)

    private val tags = listOf(
        PhoneTag("이서준", TagKind.PERSON), PhoneTag("한지우", TagKind.PERSON),
        PhoneTag("협력사 A", TagKind.ORGANIZATION), PhoneTag("네트워크", TagKind.AREA), PhoneTag("총무", TagKind.AREA),
    )

    private fun snap(vararg threads: PhoneThread) = PhoneSnapshot(threads.toList(), tags, emptyList())

    // ── fact / history ──────────────────────────────────────────────────────

    @Test fun explicitPresentationWinsOverLegacyRules() {
        assertTrue(thread("a", ThreadKind.RECORD).copy(presentation = Presentation.HISTORY).isHistory())
        assertFalse(thread("b", ThreadKind.EXECUTION).copy(presentation = Presentation.FACT).isHistory())
    }

    @Test fun legacySnapshotsFallBackToKindAndRelationRules() {
        assertTrue(thread("a", ThreadKind.EXECUTION).isHistory())
        assertTrue(thread("b", ThreadKind.TAG_CORRECTION).isHistory())
        assertTrue(thread("c", ThreadKind.REJECTION).isHistory())
        assertTrue(thread("d").copy(retracts = "x").isHistory())
        assertTrue(thread("wiki-field:1").isHistory())
        assertTrue(thread("wiki:p").copy(field = "status", content = "status 변경").isHistory())
        assertFalse(thread("wiki:p").copy(field = "status", content = "진행 중으로 바꿈").isHistory())
        assertFalse(thread("e", ThreadKind.REQUEST).isHistory())
    }

    // ── 지금: 기한 지남 / 오늘 / 새로 반영됨 (Asia/Seoul) ──────────────────────

    @Test fun groupsOverdueTodayAndRecentByKoreanDay() {
        val now = kst("2026-11-18", "09:00")
        val overdue = thread("overdue", ThreadKind.REQUEST).copy(deadline = LocalDate.parse("2026-11-17"))
        val deadlineToday = thread("due-today", ThreadKind.REQUEST).copy(deadline = LocalDate.parse("2026-11-18"))
        val meeting = thread("meeting", ThreadKind.SCHEDULE).copy(scheduleAt = kst("2026-11-18", "15:00"))
        val earlier = thread("earlier", ThreadKind.SCHEDULE).copy(scheduleAt = kst("2026-11-18", "08:00"))
        val future = thread("future", ThreadKind.REQUEST).copy(deadline = LocalDate.parse("2026-11-19"))
        val recent = thread("recent", at = kst("2026-11-17", "17:20"))
        val old = thread("old", at = kst("2026-11-16", "23:59"))

        val view = nowView(snap(overdue, deadlineToday, meeting, earlier, future, recent, old), now)

        assertEquals(listOf("overdue"), view.overdue.map { it.thread.id })
        assertEquals(listOf("earlier", "meeting", "due-today"), view.today.map { it.thread.id })
        assertEquals(DueKind.SCHEDULE, view.today.first().dueKind)
        assertEquals(listOf("recent"), view.recent.map { it.thread.id })
    }

    @Test fun dayBoundaryFollowsSeoulNotUtc() {
        // 2026-11-17T15:30Z is already 11/18 00:30 in Seoul.
        val now = Instant.parse("2026-11-17T15:30:00Z")
        val due = thread("due", ThreadKind.REQUEST).copy(deadline = LocalDate.parse("2026-11-17"))
        val late = thread("late", ThreadKind.SCHEDULE).copy(scheduleAt = Instant.parse("2026-11-17T16:00:00Z")) // 11/18 01:00 KST
        val view = nowView(snap(due, late), now)
        assertEquals(listOf("due"), view.overdue.map { it.thread.id })
        assertEquals(listOf("late"), view.today.map { it.thread.id })
        // The same instant in UTC would put both on 11/17.
        val utc = nowView(snap(due, late), now, ZoneId.of("UTC"))
        assertEquals(setOf("due", "late"), utc.today.map { it.thread.id }.toSet())
        assertTrue(utc.overdue.isEmpty())
    }

    @Test fun resolvedRetractedAndHistoryRowsNeverCountAsOpenWork() {
        val now = kst("2026-11-18", "09:00")
        val open = thread("open", ThreadKind.REQUEST).copy(deadline = LocalDate.parse("2026-11-10"))
        val done = thread("done", ThreadKind.REQUEST).copy(deadline = LocalDate.parse("2026-11-10"))
        val retracted = thread("gone", ThreadKind.REQUEST).copy(deadline = LocalDate.parse("2026-11-10"))
        val resolveRow = thread("r1", ThreadKind.EXECUTION).copy(presentation = Presentation.HISTORY, resolves = "done")
        val retractRow = thread("r2").copy(presentation = Presentation.HISTORY, retracts = "gone")
        val historyWithDeadline = thread("h", ThreadKind.REQUEST).copy(presentation = Presentation.HISTORY, deadline = LocalDate.parse("2026-11-01"))

        val view = nowView(snap(open, done, retracted, resolveRow, retractRow, historyWithDeadline), now)

        assertEquals(listOf("open"), view.overdue.map { it.thread.id })
        assertTrue(view.recent.none { it.thread.presentation == Presentation.HISTORY })
    }

    @Test fun recentExcludesRowsAlreadyShownAbove() {
        val now = kst("2026-11-18", "09:00")
        val both = thread("both", ThreadKind.REQUEST, at = kst("2026-11-18", "08:00")).copy(deadline = LocalDate.parse("2026-11-18"))
        val view = nowView(snap(both), now)
        assertEquals(listOf("both"), view.today.map { it.thread.id })
        assertTrue(view.recent.isEmpty())
    }

    @Test fun statusStripUsesTheMapsStates() {
        fun item(status: String, node: MapNode = MapNode.SPINNER) = MapItem(status, status, "call", node, status, emptyMap())
        val counts = listOf(
            item("review", MapNode.USER), item("review", MapNode.USER), item("apply_stopped"), item("working"),
            item("applying"), item("waiting"), item("done"), item("rejected"),
        ).statusCounts()
        assertEquals(StatusCounts(waitingReview = 2, processing = 3, stopped = 1), counts)
    }

    @Test fun onlyOpenRequestsCanBeOverdue() {
        val now = kst("2026-11-18", "09:00")
        val past = LocalDate.parse("2026-11-10")
        val request = thread("req", ThreadKind.REQUEST).copy(deadline = past)
        val decision = thread("dec", ThreadKind.DECISION).copy(deadline = past)
        val record = thread("rec").copy(deadline = LocalDate.parse("2026-11-18"))
        val view = nowView(snap(request, decision, record), now)
        assertEquals(listOf("req"), view.overdue.map { it.thread.id })
        assertTrue(view.today.isEmpty())
    }

    // ── 실효 값: updates / retracts / 태그정정 ───────────────────────────────

    private fun history(id: String, at: String, block: PhoneThread.() -> PhoneThread) =
        block(thread(id, ThreadKind.REQUEST, at = kst(at, "12:00")).copy(presentation = Presentation.HISTORY))

    @Test fun deadlineUpdatesFoldChronologicallyAndLastOneWins() {
        val now = kst("2026-11-18", "09:00")
        val request = thread("req", ThreadKind.REQUEST, at = kst("2026-11-01", "10:00")).copy(deadline = LocalDate.parse("2026-11-10"))
        // Listed out of order on purpose: the later change (11/18) must win over the earlier one (11/25).
        val later = history("u2", "2026-11-12") { copy(updates = "req", field = "deadline", deadline = LocalDate.parse("2026-11-18")) }
        val earlier = history("u1", "2026-11-11") { copy(updates = "req", field = "deadline", deadline = LocalDate.parse("2026-11-25")) }
        val view = nowView(snap(request, later, earlier), now)
        assertTrue(view.overdue.isEmpty())
        assertEquals(listOf("req"), view.today.map { it.thread.id })
        // Pushed into the future, the request leaves 지금 completely.
        assertTrue(nowView(snap(request, earlier), now).let { it.overdue.isEmpty() && it.today.isEmpty() })
    }

    @Test fun retractedDeadlineChangeIsSkipped() {
        val now = kst("2026-11-18", "09:00")
        val request = thread("req", ThreadKind.REQUEST, at = kst("2026-11-01", "10:00")).copy(deadline = LocalDate.parse("2026-11-10"))
        val change = history("u1", "2026-11-11") { copy(updates = "req", field = "deadline", deadline = LocalDate.parse("2026-12-31")) }
        val retraction = history("r1", "2026-11-12") { copy(retracts = "u1") }
        val view = nowView(snap(request, change, retraction), now)
        assertEquals(listOf("req"), view.overdue.map { it.thread.id }) // original 11/10 stands
    }

    @Test fun scheduleUpdatesMoveTheEventToItsNewDay() {
        val now = kst("2026-11-18", "09:00")
        val event = thread("ev", ThreadKind.SCHEDULE).copy(scheduleAt = kst("2026-11-20", "10:00"))
        val move = history("u1", "2026-11-15") { copy(updates = "ev", field = "schedule", scheduleAt = kst("2026-11-18", "16:00")) }
        assertTrue(nowView(snap(event), now).today.isEmpty())
        assertEquals(listOf("ev"), nowView(snap(event, move), now).today.map { it.thread.id })
    }

    @Test fun tagCorrectionsMoveThreadsBetweenSubjects() {
        val fact = thread("f", counterpart = "한지우", tags = listOf("네트워크"))
        val fix = history("fix", "2026-11-12") {
            copy(kind = ThreadKind.TAG_CORRECTION, correction = TagCorrection("f", add = listOf("총무", "이서준"), remove = listOf("네트워크")))
        }
        val snapshot = snap(fact, fix)
        assertTrue(timeline(snapshot, "네트워크").isEmpty())
        assertEquals(listOf("f"), timeline(snapshot, "총무").map { it.thread.id })
        // The added person tag makes it a mention of 이서준 (한지우 stays counterpart).
        assertTrue(timeline(snapshot, "이서준").isEmpty())
        assertEquals(Membership.MENTION, timeline(snapshot, "이서준", includeMentions = true).single().membership)
        // A retracted correction changes nothing.
        val undo = history("undo", "2026-11-13") { copy(retracts = "fix") }
        assertEquals(listOf("f"), timeline(snap(fact, fix, undo), "네트워크").map { it.thread.id })
    }

    @Test fun resolvesCountOnlyFromLiveNonRetractionRows() {
        val now = kst("2026-11-18", "09:00")
        val past = LocalDate.parse("2026-11-10")
        val a = thread("a", ThreadKind.REQUEST).copy(deadline = past)
        val b = thread("b", ThreadKind.REQUEST).copy(deadline = past)
        val c = thread("c", ThreadKind.REQUEST).copy(deadline = past)
        val resolvesA = history("ra", "2026-11-12") { copy(resolves = "a") }
        // Resolves b but is itself retracted: b stays open.
        val resolvesB = history("rb", "2026-11-12") { copy(resolves = "b") }
        val retractResolver = history("x", "2026-11-13") { copy(retracts = "rb") }
        // A retraction row that also carries `resolves` must not resolve anything (web relations()).
        val retractAndResolve = history("rc", "2026-11-12") { copy(retracts = "zzz", resolves = "c") }
        val view = nowView(snap(a, b, c, resolvesA, resolvesB, retractResolver, retractAndResolve), now)
        assertEquals(setOf("b", "c"), view.overdue.map { it.thread.id }.toSet())
    }

    // ── 사람·영역 타임라인 ────────────────────────────────────────────────────

    private val seo = thread("c1", ThreadKind.REQUEST, counterpart = "이서준", at = kst("2026-11-17", "10:00"))
    private val mention = thread("m1", counterpart = "한지우", tags = listOf("이서준", "네트워크"), at = kst("2026-11-13", "10:00"))
    private val both = thread("b1", counterpart = "이서준", tags = listOf("이서준"), at = kst("2026-11-12", "10:00"))
    private val other = thread("o1", counterpart = "한지우", tags = listOf("네트워크"), at = kst("2026-11-11", "10:00"))

    @Test fun personDefaultsToCounterpartThreadsOnly() {
        val rows = timeline(snap(seo, mention, both, other), "이서준")
        assertEquals(listOf("c1", "b1"), rows.map { it.thread.id })
        assertTrue(rows.all { it.membership == Membership.COUNTERPART })
    }

    @Test fun mentionOnlyThreadsJoinWhenRequestedAndAreMarked() {
        val rows = timeline(snap(seo, mention, both, other), "이서준", includeMentions = true)
        assertEquals(listOf("c1", "m1", "b1"), rows.map { it.thread.id })
        assertEquals(Membership.MENTION, rows.single { it.thread.id == "m1" }.membership)
        // Being tagged and the counterpart at once is a counterpart thread, not a mention.
        assertEquals(Membership.COUNTERPART, rows.single { it.thread.id == "b1" }.membership)
    }

    @Test fun mentionOnlyMirrorsWebMentionMatch() {
        val parties = setOf("이서준", "한지우")
        assertTrue(mentionOnly(mention, "이서준", parties))
        assertFalse(mentionOnly(both, "이서준", parties))
        assertFalse(mentionOnly(other, "이서준", parties))
        // Areas are never "mention-only".
        assertFalse(mentionOnly(other, "네트워크", parties))
    }

    @Test fun organizationsFollowTheSameRule() {
        val org = thread("org", counterpart = "협력사 A")
        val tagged = thread("tagged", counterpart = "이서준", tags = listOf("협력사 A"))
        assertEquals(listOf("org"), timeline(snap(org, tagged), "협력사 A").map { it.thread.id })
        assertEquals(listOf("org", "tagged").toSet(), timeline(snap(org, tagged), "협력사 A", includeMentions = true).map { it.thread.id }.toSet())
    }

    @Test fun areaTimelineMatchesByTagAndIgnoresMentionToggle() {
        val rows = timeline(snap(seo, mention, both, other), "네트워크", includeMentions = true)
        assertEquals(listOf("m1", "o1"), rows.map { it.thread.id })
        assertTrue(rows.all { it.membership == Membership.AREA })
    }

    @Test fun historyIsHiddenByDefaultAndFollowsItsTargetWhenIncluded() {
        val exec = thread("e1", ThreadKind.EXECUTION, tags = emptyList(), at = kst("2026-11-18", "09:00"))
            .copy(presentation = Presentation.HISTORY, executes = "c1")
        val unrelated = thread("e2", ThreadKind.EXECUTION, counterpart = "한지우").copy(presentation = Presentation.HISTORY)
        val snapshot = snap(seo, both, exec, unrelated)

        assertEquals(listOf("c1", "b1"), timeline(snapshot, "이서준").map { it.thread.id })

        val withHistory = timeline(snapshot, "이서준", includeHistory = true)
        assertEquals(listOf("c1", "e1", "b1"), withHistory.map { it.thread.id })
        assertEquals(listOf(false, true, false), withHistory.map { it.isHistory })
    }

    @Test fun cyclicHistoryTerminates() {
        val a = thread("a", counterpart = "이서준").copy(presentation = Presentation.HISTORY, retracts = "b")
        val b = thread("b", counterpart = "이서준").copy(presentation = Presentation.HISTORY, retracts = "a")
        assertEquals(setOf("a", "b"), timeline(snap(a, b), "이서준", includeHistory = true).map { it.thread.id }.toSet())
    }

    // ── 링크, 디렉터리, 합성 데이터 ──────────────────────────────────────────

    @Test fun wikiLinksResolveToPeopleAreasOrPages() {
        val page = WikiPage("w1", "방화벽 장비 교체", TagKind.PROJECT, null, LocalDate.parse("2026-11-17"), body = "")
        val snapshot = PhoneSnapshot(emptyList(), tags, listOf(page))
        assertEquals(LinkTarget.Person("이서준"), resolveLink(snapshot, "이서준"))
        assertEquals(LinkTarget.Person("협력사 A"), resolveLink(snapshot, "협력사 A"))
        assertEquals(LinkTarget.Area("네트워크"), resolveLink(snapshot, "네트워크"))
        assertEquals(LinkTarget.Page("w1"), resolveLink(snapshot, "방화벽 장비 교체"))
        assertEquals(LinkTarget.None, resolveLink(snapshot, "없는 이름"))
    }

    @Test fun syntheticModelFillsEveryNowSectionAndOnlyUsesKnownTags() = runBlocking {
        val clock = Clock.fixed(kst("2026-11-18", "09:00"), ZoneId.of("UTC"))
        val snapshot = SyntheticPhoneReadModel(clock).snapshot()
        val view = nowView(snapshot, clock.instant())
        assertEquals(listOf("협력사 A 견적서 회신"), view.overdue.map { it.thread.content })
        assertEquals(setOf("스위치 교체 사전 점검", "임원실 프린터 드라이버 배포"), view.today.map { it.thread.content }.toSet())
        assertTrue(view.recent.isNotEmpty())
        // The finished request is closed by its history row, so it is not overdue.
        assertTrue(view.overdue.none { it.thread.content == "VPN 계정 만료 연장" })
        val known = snapshot.tags.map { it.name }.toSet()
        snapshot.threads.flatMap { it.tags + listOfNotNull(it.counterpart) }.forEach { assertTrue(it, it in known) }
        assertTrue(timeline(snapshot, "이서준", includeMentions = true).any { it.membership == Membership.MENTION })
    }

    @Test fun wikiMarkdownParsesStructureAndLinks() {
        val blocks = parseWikiBody("## 진행\n- [[이서준]]과 **협의**\n- [[이서준|서준]] 확인\n\n첫 줄\n둘째 줄")
        assertEquals(4, blocks.size)
        assertTrue(blocks[0] is MdBlock.Heading)
        val bullet = blocks[1] as MdBlock.Bullet
        assertEquals(listOf(Inline.Link("이서준", "이서준"), Inline.Text("과 "), Inline.Bold("협의")), bullet.text)
        assertEquals(Inline.Link("이서준", "서준"), (blocks[2] as MdBlock.Bullet).text.first())
        assertEquals(listOf<Inline>(Inline.Text("첫 줄 둘째 줄")), (blocks[3] as MdBlock.Paragraph).text)
    }

    @Test fun releaseWiringNeverUsesSyntheticData() = runBlocking {
        val release = PhoneReadModels.forBuild(debug = false)
        assertFalse(release.isExample)
        assertTrue(release.snapshot().isEmpty)
        assertTrue(PhoneReadModels.forBuild(debug = true).isExample)
    }
}
