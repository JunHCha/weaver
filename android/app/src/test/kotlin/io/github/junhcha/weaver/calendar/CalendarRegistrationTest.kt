package io.github.junhcha.weaver.calendar

import io.github.junhcha.weaver.schema.Action
import io.github.junhcha.weaver.schema.ActionKind
import io.github.junhcha.weaver.schema.ActionResult
import io.github.junhcha.weaver.schema.CalendarPayload
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.time.Instant
import java.time.OffsetDateTime

class CalendarRegistrationTest {
    @get:Rule val temporary = TemporaryFolder()
    private val target = CalendarTarget(42, "Synthetic", "test@example.invalid", "LOCAL")
    private fun id(n: Int) = "act_" + n.toString().padStart(32, '0')
    private fun payload(start: String = "2026-01-01T09:00:00+09:00", end: String? = null, zone: String = "Asia/Seoul", title: String = "회의") =
        CalendarPayload(start, end, zone, title)
    private fun register(n: Int = 1, payload: CalendarPayload = payload()) =
        Action(2, id(n), ActionKind.REGISTER, "thread-$n", n.toLong(), 1, payload, null)
    private fun update(n: Int, event: String, payload: CalendarPayload) =
        Action(2, id(n), ActionKind.UPDATE, "thread-$n", n.toLong(), 2, payload, event)
    private fun remove(n: Int, event: String) =
        Action(2, id(n), ActionKind.REMOVE, "thread-$n", n.toLong(), 3, payload(), event)

    private class Provider : CalendarProvider {
        var targets = listOf(CalendarTarget(42, "Synthetic", "test@example.invalid", "LOCAL"))
        val events = mutableMapOf<String, List<CalendarRow>>()
        val values = mutableMapOf<String, CalendarEvent>()
        val usedTargets = mutableListOf<CalendarTarget>()
        val updates = mutableListOf<Pair<String, CalendarEvent>>()
        val deletes = mutableListOf<String>()
        var inserts = 0
        var failQuery = false
        var failInsert = false
        var denied = false
        var lostResponse = false
        var crash = false
        override fun calendars(): List<CalendarTarget> {
            if (denied) throw SecurityException("synthetic revoked permission")
            return targets
        }
        override fun find(marker: String): List<CalendarRow> {
            if (failQuery) error("synthetic provider failure")
            return events[marker].orEmpty()
        }
        override fun insert(target: CalendarTarget, marker: String, event: CalendarEvent): Pair<String, Long> {
            if (failInsert) throw SecurityException("synthetic revoked permission")
            inserts++; usedTargets += target
            val row = "content://com.android.calendar/events/$inserts" to inserts.toLong()
            values[row.first] = event
            events[marker] = listOf(CalendarRow(row.first, row.second, target.id, event))
            if (crash) throw AssertionError("simulated process death after provider commit")
            if (lostResponse) error("synthetic lost response")
            return row
        }
        override fun update(uri: String, event: CalendarEvent): Boolean {
            if (failQuery) error("synthetic provider failure")
            if (uri !in values) return false
            updates += uri to event; values[uri] = event; return true
        }
        override fun delete(uri: String): Boolean {
            deletes += uri; return values.remove(uri) != null
        }
    }
    private var clock = 1_000_000L
    private fun journal() = CalendarJournal(File(temporary.root, "registrations.json"))
    private fun executor(provider: Provider, maxAttempts: Int = 5) = ActionExecutor(journal(), provider, { clock }, maxAttempts)
    private fun results(provider: Provider, vararg actions: Action) = executor(provider).run(actions.toList())
    private fun state(n: Int) = checkNotNull(journal().action(id(n)))

    @Test fun waitsWithoutSideEffectsUntilTheUserChoosesACalendar() {
        val provider = Provider()
        assertTrue(results(provider, register()).isEmpty())
        assertEquals(0, provider.inserts)
        assertEquals(ActionPhase.PENDING, state(1).phase)
        assertEquals("target", state(1).note)
        assertEquals(0, state(1).attempts)
        journal().select(target)
        val done = results(provider, register()).single()
        assertEquals(ActionResult.Outcome.COMPLETED, done.outcome)
        assertEquals("content://com.android.calendar/events/1", done.externalId)
        assertEquals(1, provider.inserts)
    }

    @Test fun registersAutomaticallyOnceAndReportsTheSameResultEveryTime() {
        val provider = Provider()
        journal().select(target)
        val first = results(provider, register()).single()
        clock += 60_000
        val again = results(provider, register()).single()
        assertEquals(first, again) // byte-identical: finishedAt is fixed when it became final
        assertEquals(1, provider.inserts)
        assertEquals(ActionPhase.DONE, state(1).phase)
        assertEquals(1L, first.attempts)
        assertEquals(1L, journal().mappings().single().rowId)
        assertEquals(id(1), journal().mappings().single().threadId)
        assertEquals(Instant.ofEpochMilli(1_000_000L).toString(), first.finishedAt)
    }

    @Test fun usesTheSelectedCalendarAndAnotherActionNeverReusesAnExistingRow() {
        val provider = Provider()
        val other = target.copy(id = 99)
        provider.targets = listOf(target, other)
        journal().select(other)
        results(provider, register(1), register(2))
        assertEquals(listOf(other, other), provider.usedTargets)
        assertEquals(2, provider.inserts)
        assertEquals(2, journal().mappings().map { it.rowId }.distinct().size)
    }

    @Test fun crashAfterInsertRecoversByMarkerWithoutSecondInsert() {
        val provider = Provider().apply { crash = true }
        journal().select(target)
        val death = assertThrows(AssertionError::class.java) { results(provider, register()) }
        assertEquals("simulated process death after provider commit", death.message)
        assertNull(journal().mappings().single().uri)
        provider.crash = false
        val done = results(provider, register()).single()
        assertEquals(ActionResult.Outcome.COMPLETED, done.outcome)
        assertEquals(1, provider.inserts)
        assertNotNull(journal().mappings().single().uri)
    }

    @Test fun queryFailureNeverGuessesAbsenceAndTransientFailuresRetryWithBackoff() {
        val provider = Provider().apply { failQuery = true }
        journal().select(target)
        assertTrue(results(provider, register()).isEmpty())
        assertEquals(0, provider.inserts)
        assertEquals(ActionPhase.RETRY, state(1).phase)
        assertEquals(1, state(1).attempts)
        // Not due yet: nothing is attempted.
        provider.failQuery = false
        assertTrue(results(provider, register()).isEmpty())
        assertEquals(0, provider.inserts)
        clock += 61_000
        provider.lostResponse = true
        assertTrue(results(provider, register()).isEmpty())
        assertEquals(2, state(1).attempts)
        clock += 121_000
        provider.lostResponse = false
        val done = results(provider, register()).single()
        assertEquals(ActionResult.Outcome.COMPLETED, done.outcome)
        assertEquals(3L, done.attempts)
        assertEquals(1, provider.inserts)
    }

    @Test fun givesUpAfterTheLastAttemptAndReportsAFailure() {
        val provider = Provider().apply { failQuery = true }
        journal().select(target)
        var last: List<ActionResult> = emptyList()
        repeat(5) {
            clock += 4_000_000
            last = executor(provider).run(listOf(register()))
        }
        val failed = last.single()
        assertEquals(ActionResult.Outcome.FAILED, failed.outcome)
        assertEquals("provider_error", failed.error)
        assertEquals(5L, failed.attempts)
        assertNull(failed.externalId)
        assertEquals(0, provider.inserts)
        clock += 4_000_000
        provider.failQuery = false
        assertEquals(failed, results(provider, register()).single()) // final: never tried again
        assertEquals(0, provider.inserts)
    }

    @Test fun missingCalendarPermissionWaitsWithoutUsingAttempts() {
        val provider = Provider().apply { denied = true }
        journal().select(target)
        repeat(7) { assertTrue(results(provider, register()).isEmpty()) }
        assertEquals(0, state(1).attempts)
        assertEquals("permission", state(1).note)
        provider.denied = false
        assertEquals(ActionResult.Outcome.COMPLETED, results(provider, register()).single().outcome)
    }

    @Test fun removedCalendarTargetWaitsForANewSelectionAndSendsNothing() {
        val provider = Provider()
        provider.targets = emptyList()
        journal().select(target)
        assertTrue(results(provider, register()).isEmpty())
        assertEquals(0, provider.inserts)
        assertEquals("target", state(1).note)
        assertEquals(0, state(1).attempts)
    }

    @Test fun copiedMarkerAndMultipleMatchesNeedManualReviewAndNeverInsert() {
        val provider = Provider().apply { failQuery = true }
        journal().select(target)
        executor(provider).run(listOf(register()))
        val pending = journal().mappings().single()
        val event = pending.event!!
        provider.failQuery = false
        for (changed in listOf(event.copy(title = "Copied title"), event.copy(start = event.start + 1),
            event.copy(end = event.end + 1), event.copy(timezone = "America/New_York"))) {
            provider.events[pending.marker] = listOf(CalendarRow("content://com.android.calendar/events/999", 999, target.id, changed))
            val failed = executor(provider).run(listOf(reopen())).single()
            assertEquals("manual_review", failed.error)
            assertNull(journal().mappings().single().uri)
        }
        assertEquals(0, provider.inserts)
    }

    /** Forget the final state so the same pending marker is judged again, as a repeated recovery would. */
    private fun reopen(): Action {
        journal().put(ActionState(id(1), ActionKind.REGISTER, "회의"))
        return register()
    }

    @Test fun ambiguousFindsFailWithoutAnInsert() {
        val provider = Provider().apply { failQuery = true }
        journal().select(target)
        executor(provider).run(listOf(register()))
        val pending = journal().mappings().single()
        provider.failQuery = false
        val row = CalendarRow("content://com.android.calendar/events/999", 999, target.id, pending.event)
        for (rows in listOf(listOf(row, row.copy(id = 1000)), listOf(row.copy(calendarId = 99)))) {
            provider.events[pending.marker] = rows
            assertEquals("manual_review", executor(provider).run(listOf(reopen())).single().error)
        }
        provider.events.clear(); journal().put(pending.copy(event = null))
        assertEquals("manual_review", executor(provider).run(listOf(reopen())).single().error)
        assertEquals(0, provider.inserts)
    }

    @Test fun updateMovesTheKnownEventAndRepeatingItIsHarmless() {
        val provider = Provider()
        journal().select(target)
        val event = checkNotNull(results(provider, register()).single().externalId)
        val moved = payload(start = "2026-01-02T09:00:00+09:00")
        val done = results(provider, update(2, event, moved)).single()
        assertEquals(ActionResult.Outcome.COMPLETED, done.outcome)
        assertEquals(event, done.externalId)
        assertEquals(CalendarEvent.from(moved), provider.values.getValue(event))
        assertEquals(CalendarEvent.from(moved), journal().mappings().single().event)
        results(provider, update(2, event, moved))
        assertEquals(1, provider.updates.size) // final state is reported, not re-applied
        assertEquals(1, provider.inserts)
    }

    @Test fun updateOfAVanishedEventFailsWithoutRetry() {
        val provider = Provider()
        journal().select(target)
        val failed = results(provider, update(2, "content://com.android.calendar/events/404", payload())).single()
        assertEquals("event_missing", failed.error)
        assertEquals(1L, failed.attempts)
    }

    @Test fun removeDeletesTheKnownEventAndIsDoneEvenIfAlreadyGone() {
        val provider = Provider()
        journal().select(target)
        val event = checkNotNull(results(provider, register()).single().externalId)
        val done = results(provider, remove(2, event)).single()
        assertEquals(ActionResult.Outcome.COMPLETED, done.outcome)
        assertEquals(listOf(event), provider.deletes)
        assertTrue(journal().mappings().single().removed)
        val gone = results(provider, remove(3, event)).single()
        assertEquals(ActionResult.Outcome.COMPLETED, gone.outcome)
    }

    @Test fun actionsRunInSequenceOrder() {
        val provider = Provider()
        journal().select(target)
        val out = results(provider, register(2, payload(title = "둘째")), register(1, payload(title = "첫째")))
        assertEquals(listOf(id(1), id(2)), out.map { it.actionId })
        assertEquals(listOf("첫째", "둘째"), provider.values.keys.sorted().map { provider.values.getValue(it).title })
    }

    @Test fun seoulMidnightAndDstOffsetsRemainTimedWithOneHourFallback() {
        for ((start, end, zone) in listOf(
            Triple("2026-11-08T00:00:00+09:00", null, "Asia/Seoul"),
            Triple("2026-03-08T01:30:00-05:00", "2026-03-08T03:30:00-04:00", "America/New_York"),
            Triple("2026-11-01T01:30:00-04:00", "2026-11-01T01:30:00-05:00", "America/New_York"),
        )) {
            val event = CalendarEvent.from(payload(start = start, end = end, zone = zone))
            assertEquals(OffsetDateTime.parse(start).toInstant().toEpochMilli(), event.start)
            assertEquals(3_600_000L, event.end - event.start)
            assertEquals(zone, event.timezone)
        }
        val event = CalendarEvent.from(payload(end = "2026-01-01T10:30:00+09:00"))
        assertEquals(Instant.parse("2026-01-01T01:30:00Z").toEpochMilli(), event.end)
    }

    @Test fun journalsFromTheManualFlowStillLoadAndNeverMatchAnAction() {
        File(temporary.root, "registrations.json").writeText("""{"mappings":[{"proposalId":"v2","rev":1,"ref":"thread-1",
            "calendar":{"id":42,"name":"Synthetic","account":"test@example.invalid","type":"LOCAL"},"marker":"m","uri":"content://x/1","rowId":1}],
            "selected":{"id":42,"name":"Synthetic","account":"test@example.invalid","type":"LOCAL"}}""")
        val provider = Provider()
        val done = results(provider, register(1)).single()
        assertEquals(ActionResult.Outcome.COMPLETED, done.outcome)
        assertEquals(1, provider.inserts) // an old thread-keyed mapping is not this action
        assertEquals(2, journal().mappings().size)
    }
}
