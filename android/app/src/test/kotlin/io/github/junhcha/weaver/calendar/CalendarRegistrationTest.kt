package io.github.junhcha.weaver.calendar

import io.github.junhcha.weaver.review.*
import io.github.junhcha.weaver.schema.*
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.time.Instant

class CalendarRegistrationTest {
    @get:Rule val temporary = TemporaryFolder()
    private val target = CalendarTarget(42, "Synthetic", "test@example.invalid", "LOCAL")
    private val original = WeaverCodec.json.decodeFromString<Proposal>(
        File(System.getProperty("weaver.schemaDir"), "examples/Proposal.json").readText())
    private fun proposal(blocked: Boolean = false): Proposal = original.copy(
        entities = original.entities.map { it.copy(ask = null) },
        schedules = original.schedules.map { it.copy(blockedByRequest = if (blocked) "d1" else null) },
        entryDrafts = original.entryDrafts.map { it.copy(blockedByRequest = null) })
    private fun record(p: Proposal = proposal(), kind: Verdict.Kind = Verdict.Kind.APPROVE, published: Boolean = true,
                       edits: List<Verdict.Edit> = emptyList()) = ReviewRecord(
        Verdict(1, p.proposalId, p.rev, kind, edits,
            if (kind == Verdict.Kind.REJECT) Verdict.RejectReason.OTHER else null,
            if (kind == Verdict.Kind.REVISE) "Synthetic revision" else null, "2026-11-08T00:00:00Z"),
        "synthetic", ReviewStatus.SENT, published, p)
    private class Provider : CalendarProvider {
        var targets = listOf(CalendarTarget(42, "Synthetic", "test@example.invalid", "LOCAL"))
        val events = mutableMapOf<String, List<CalendarRow>>()
        val values = mutableListOf<CalendarEvent>()
        val usedTargets = mutableListOf<CalendarTarget>()
        var inserts = 0
        var failQuery = false
        var failInsert = false
        var lostResponse = false
        var crash = false
        override fun calendars() = targets
        override fun find(marker: String): List<CalendarRow> {
            if (failQuery) error("synthetic provider failure")
            return events[marker].orEmpty()
        }
        override fun insert(target: CalendarTarget, marker: String, event: CalendarEvent): Pair<String, Long> {
            if (failInsert) throw SecurityException("synthetic revoked permission")
            inserts++; values += event; usedTargets += target
            val row = "content://com.android.calendar/events/$inserts" to inserts.toLong()
            events[marker] = listOf(CalendarRow(row.first, row.second, target.id, event))
            if (crash) throw AssertionError("simulated process death after provider commit")
            if (lostResponse) error("synthetic lost response")
            return row
        }
    }
    private fun journal() = CalendarJournal(File(temporary.root, "registrations.json"))
    private fun registration(provider: Provider) = CalendarRegistration(journal(), provider)

    @Test fun v2RegistrationIsIdempotentAcrossRevisionsAndHonorsBlocksAndRetraction() {
        val provider = Provider()
        val service = registration(provider)
        val schedule = io.github.junhcha.weaver.schema.v2.ApprovedSchedule(
            2, "package", "thread", "회의", "2026-01-01T09:00:00+09:00", null,
            "Asia/Seoul", "request", "a".repeat(64), 1, false)
        assertTrue(service.registerV2(schedule, target).contains("request"))
        assertEquals(0, provider.inserts)
        assertTrue(service.registerV2(schedule.copy(blockedBy = null), target).contains("등록됨"))
        assertTrue(service.registerV2(schedule.copy(blockedBy = null, revision = 2), target).contains("등록됨"))
        assertEquals(1, provider.inserts)
        assertTrue(service.registerV2(schedule.copy(blockedBy = null, retracted = true), target).contains("등록 해제"))
        assertEquals(1, provider.inserts)
    }

    @Test fun unpublishedRejectReviseAndBlockedSchedulesNeverWrite() {
        val provider = Provider()
        val service = registration(provider)
        assertTrue(service.register(record(published = false), target, "Asia/Seoul").isEmpty())
        for (kind in listOf(Verdict.Kind.REJECT, Verdict.Kind.REVISE)) {
            assertTrue(service.register(record(kind = kind), target, "Asia/Seoul").isEmpty())
        }
        assertTrue(service.register(record(proposal(true)), target, "Asia/Seoul").single().contains("등록 차단"))
        assertNull(CalendarRegistration.candidate(record().copy(proposal = null)))
        assertEquals(0, provider.inserts)
        assertTrue(journal().mappings().isEmpty())
        // Alert has no entry point: only a published ReviewRecord with its snapshot is accepted.
    }

    @Test fun approvalUsesOriginalAndEditUsesFinalPointerValues() {
        val provider = Provider()
        registration(provider).register(record(), target, "Asia/Seoul")
        assertEquals(proposal().schedules.single().title, provider.values.single().title)
        val edited = proposal().copy(rev = 2)
        val edits = listOf(Verdict.Edit("/schedules/0/title", JsonPrimitive("Synthetic edited title")),
            Verdict.Edit("/schedules/0/start", JsonPrimitive("2026-11-08T17:00:00+09:00")))
        registration(provider).register(record(edited, Verdict.Kind.EDIT, edits = edits), target, "Asia/Seoul")
        assertEquals("Synthetic edited title", provider.values.last().title)
        assertEquals(Instant.parse("2026-11-08T08:00:00Z").toEpochMilli(), provider.values.last().start)
        assertEquals(2, provider.inserts)
    }

    @Test fun displayedTargetIsUsedEvenIfAnotherCardChangedTheRememberedDefault() {
        val provider = Provider()
        val other = target.copy(id = 99)
        provider.targets = listOf(target, other)
        journal().select(other)
        registration(provider).register(record(), target, "Asia/Seoul")
        assertEquals(listOf(target), provider.usedTargets)
    }

    @Test fun blockedItemDoesNotPreventOtherApprovedSchedules() {
        val p = proposal(true)
        val mixed = p.copy(schedules = p.schedules + p.schedules.single().copy(ref = "s2", blockedByRequest = null))
        val provider = Provider()
        val results = registration(provider).register(record(mixed), target, "Asia/Seoul")
        assertTrue(results.first().contains("등록 차단"))
        assertTrue(results.last().contains("등록됨"))
        assertEquals(1, provider.inserts)
        assertEquals("s2", journal().mappings().single().ref)
    }

    @Test fun repeatTapAndJournalReopenKeepUniqueMappingAndSelection() {
        val provider = Provider()
        journal().select(target)
        repeat(3) { registration(provider).register(record(), target, "Asia/Seoul") }
        assertEquals(1, provider.inserts)
        assertEquals(target, journal().selected())
        assertEquals(1L, journal().mappings().single().rowId)
        assertNotNull(journal().mappings().single().uri)
    }

    @Test fun crashAfterInsertRecoversByMarkerWithoutSecondInsert() {
        val provider = Provider().apply { crash = true }
        val death = assertThrows(AssertionError::class.java) {
            registration(provider).register(record(), target, "Asia/Seoul")
        }
        assertEquals("simulated process death after provider commit", death.message)
        assertNull(journal().mappings().single().uri)
        provider.crash = false
        registration(provider).recover(record())
        registration(provider).register(record(), target, "Asia/Seoul")
        assertEquals(1, provider.inserts)
        assertNotNull(journal().mappings().single().uri)
    }

    @Test fun queryFailureNeverGuessesAbsenceAndInsertFailureCanRetryWithoutChangingApproval() {
        val provider = Provider().apply { failQuery = true }
        val approved = record()
        registration(provider).register(approved, target, "Asia/Seoul")
        assertEquals(0, provider.inserts)
        provider.failQuery = false; provider.failInsert = true
        registration(provider).register(approved, target, "Asia/Seoul")
        assertEquals(0, provider.inserts)
        provider.failInsert = false; provider.lostResponse = true
        registration(provider).register(approved, target, "Asia/Seoul")
        provider.lostResponse = false
        registration(provider).register(approved, target, "Asia/Seoul")
        assertEquals(1, provider.inserts)
        assertTrue(approved.published)
        assertEquals(Verdict.Kind.APPROVE, approved.verdict.verdict)
    }

    @Test fun anotherProposalOrRevisionNeverReusesExistingRow() {
        val provider = Provider()
        registration(provider).register(record(), target, "Asia/Seoul")
        registration(provider).register(record(proposal().copy(proposalId = "prp_01ARZ3NDEKTSV4RRFFQ69G5FA1")), target, "Asia/Seoul")
        registration(provider).register(record(proposal().copy(rev = 2)), target, "Asia/Seoul")
        assertEquals(3, provider.inserts)
        assertEquals(3, journal().mappings().map { it.rowId }.distinct().size)
    }

    @Test fun restartBeforeInsertOnlyReconcilesAndKeepsOriginalTargetForManualRetry() {
        val provider = Provider().apply { failQuery = true }
        registration(provider).register(record(), target, "Asia/Seoul")
        assertEquals(target, journal().mappings().single().calendar)
        provider.failQuery = false
        registration(provider).recover(record())
        assertEquals(0, provider.inserts)
        val other = target.copy(id = 99)
        provider.targets = listOf(other)
        assertTrue(registration(provider).register(record(), other, "Asia/Seoul").single().contains("이전 대상 사용 불가"))
        assertEquals(0, provider.inserts) // Never send an old target id to a replacement account/calendar.
        provider.targets = listOf(target, other)
        registration(provider).register(record(), other, "Asia/Seoul")
        assertEquals(target, journal().mappings().single().calendar)
        assertEquals(1, provider.inserts)
    }

    @Test fun renamedCalendarKeepsIdentityAndPersistedEventOnRetry() {
        val provider = Provider().apply { failQuery = true }
        registration(provider).register(record(), target, "Asia/Seoul")
        val event = journal().mappings().single().event
        val renamed = target.copy(name = "Renamed")
        provider.targets = listOf(renamed); provider.failQuery = false
        registration(provider).register(record(), renamed, "America/New_York")
        assertEquals(listOf(renamed), provider.usedTargets)
        assertEquals(event, provider.values.single())
    }

    @Test fun removedOrUnwritableTargetRequiresExplicitReplacementAfterProvedAbsence() {
        val provider = Provider().apply { failQuery = true }
        registration(provider).register(record(), target, "Asia/Seoul")
        val other = target.copy(id = 99)
        provider.targets = listOf(other); provider.failQuery = false
        registration(provider).recover(record())
        assertTrue(journal().mappings().single().needsTarget)
        registration(provider).register(record(), other, "Asia/Seoul")
        assertEquals(0, provider.inserts)
        val ref = proposal().schedules.single().ref
        provider.failQuery = true
        registration(provider).register(record(), other, "Asia/Seoul", ref)
        assertEquals(0, provider.inserts)
        provider.failQuery = false
        registration(provider).register(record(), other, "Asia/Seoul", ref)
        assertEquals(listOf(other), provider.usedTargets)
        assertEquals(other, journal().mappings().single().calendar)
    }

    @Test fun copiedMarkerRequiresManualDecisionInRecoverAndRegister() {
        val provider = Provider().apply { failQuery = true }
        registration(provider).register(record(), target, "Asia/Seoul")
        val pending = journal().mappings().single()
        provider.failQuery = false
        val copied = CalendarRow("content://com.android.calendar/events/999", 999, 99,
            pending.event!!.copy(start = pending.event.start + 60_000))
        provider.events[pending.marker] = listOf(copied)
        registration(provider).recover(record())
        assertNull(journal().mappings().single().uri)
        assertTrue(journal().mappings().single().manualReview)
        // Check register independently of the state set by recover.
        journal().put(pending)
        assertTrue(registration(provider).register(record(), target, "Asia/Seoul").single().contains("수동 확인"))
        assertNull(journal().mappings().single().uri)
        assertEquals(0, provider.inserts)
        provider.events.clear()
        registration(provider).register(record(), target, "Asia/Seoul")
        assertEquals(0, provider.inserts) // Manual state remains even if the copied row disappears.
        registration(provider).register(record(), target, "Asia/Seoul", pending.ref)
        assertEquals(1, provider.inserts)
        assertNotEquals(pending.marker, journal().mappings().single().marker)
    }

    @Test fun eachChangedEventFieldRequiresManualReview() {
        val provider = Provider().apply { failQuery = true }
        registration(provider).register(record(), target, "Asia/Seoul")
        val pending = journal().mappings().single()
        val event = pending.event!!
        provider.failQuery = false
        for (changed in listOf(event.copy(title = "Copied title"), event.copy(start = event.start + 1),
            event.copy(end = event.end + 1), event.copy(timezone = "America/New_York"))) {
            journal().put(pending)
            provider.events[pending.marker] = listOf(CalendarRow("content://com.android.calendar/events/999", 999, target.id, changed))
            registration(provider).recover(record())
            assertTrue(journal().mappings().single().manualReview)
            assertNull(journal().mappings().single().uri)
        }
        assertEquals(0, provider.inserts)
    }

    @Test fun multipleMatchesAndLegacyJournalWithoutValuesRequireManualConfirmation() {
        val provider = Provider().apply { failQuery = true }
        registration(provider).register(record(), target, "Asia/Seoul")
        val pending = journal().mappings().single()
        val row = CalendarRow("content://com.android.calendar/events/999", 999, target.id, pending.event)
        provider.failQuery = false; provider.events[pending.marker] = listOf(row, row.copy(id = 1000))
        registration(provider).recover(record())
        registration(provider).register(record(), target, "Asia/Seoul")
        assertNull(journal().mappings().single().uri)
        assertTrue(journal().mappings().single().manualReview)
        provider.events.clear(); journal().put(pending.copy(event = null))
        registration(provider).register(record(), target, "Asia/Seoul")
        assertTrue(journal().mappings().single().manualReview)
        assertEquals(0, provider.inserts)
    }

    @Test fun seoulMidnightAndDstOffsetsRemainTimedWithOneHourFallback() {
        for ((start, end, zone, expectedDuration) in listOf(
            listOf("2026-11-08T00:00:00+09:00", null, "Asia/Seoul", "3600000"),
            listOf("2026-03-08T01:30:00-05:00", "2026-03-08T03:30:00-04:00", "America/New_York", "3600000"),
            listOf("2026-11-01T01:30:00-04:00", "2026-11-01T01:30:00-05:00", "America/New_York", "3600000"),
        )) {
            val event = CalendarEvent.from(proposal().schedules.single().copy(start = start!!, end = end), zone!!)
            assertEquals(Instant.parse(offsetInstant(start)).toEpochMilli(), event.start)
            assertEquals(expectedDuration!!.toLong(), event.end - event.start)
            assertEquals(zone, event.timezone)
        }
    }
    private fun offsetInstant(value: String) = java.time.OffsetDateTime.parse(value).toInstant().toString()
}
