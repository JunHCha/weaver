package io.github.junhcha.weaver.calendar

import io.github.junhcha.weaver.schema.ApprovedSchedule
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
    private fun schedule(thread: String = "thread-1", blockedBy: String? = null, revision: Long = 1, retracted: Boolean = false,
                         start: String = "2026-01-01T09:00:00+09:00", end: String? = null, zone: String = "Asia/Seoul") =
        ApprovedSchedule(2, "package-1", thread, "회의", start, end, zone, blockedBy, "a".repeat(64), revision, retracted)

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

    @Test fun registrationIsIdempotentAcrossRevisionsAndHonorsBlocksAndRetraction() {
        val provider = Provider()
        val service = registration(provider)
        assertTrue(service.register(schedule(blockedBy = "request"), target).contains("request"))
        assertEquals(0, provider.inserts)
        assertTrue(service.register(schedule(), target).contains("등록됨"))
        assertTrue(service.register(schedule(revision = 2), target).contains("등록됨"))
        assertEquals(1, provider.inserts)
        assertTrue(service.register(schedule(retracted = true), target).contains("등록 해제"))
        assertEquals(1, provider.inserts)
    }

    @Test fun displayedTargetIsUsedEvenIfAnotherCardChangedTheRememberedDefault() {
        val provider = Provider()
        val other = target.copy(id = 99)
        provider.targets = listOf(target, other)
        journal().select(other)
        registration(provider).register(schedule(), target)
        assertEquals(listOf(target), provider.usedTargets)
    }

    @Test fun blockedItemDoesNotPreventOtherApprovedSchedules() {
        val provider = Provider()
        val service = registration(provider)
        assertTrue(service.register(schedule("thread-1", blockedBy = "request"), target).contains("등록 차단"))
        assertTrue(service.register(schedule("thread-2"), target).contains("등록됨"))
        assertEquals(1, provider.inserts)
        assertEquals("thread-2", journal().mappings().single().threadId)
    }

    @Test fun repeatTapAndJournalReopenKeepUniqueMappingAndSelection() {
        val provider = Provider()
        journal().select(target)
        repeat(3) { registration(provider).register(schedule(), target) }
        assertEquals(1, provider.inserts)
        assertEquals(target, journal().selected())
        assertEquals(1L, journal().mappings().single().rowId)
        assertNotNull(journal().mappings().single().uri)
    }

    @Test fun crashAfterInsertRecoversByMarkerWithoutSecondInsert() {
        val provider = Provider().apply { crash = true }
        val death = assertThrows(AssertionError::class.java) { registration(provider).register(schedule(), target) }
        assertEquals("simulated process death after provider commit", death.message)
        assertNull(journal().mappings().single().uri)
        provider.crash = false
        assertTrue(registration(provider).register(schedule(), target).contains("등록됨"))
        assertEquals(1, provider.inserts)
        assertNotNull(journal().mappings().single().uri)
    }

    @Test fun queryFailureNeverGuessesAbsenceAndInsertFailureCanRetry() {
        val provider = Provider().apply { failQuery = true }
        assertTrue(registration(provider).register(schedule(), target).contains("등록 실패"))
        assertEquals(0, provider.inserts)
        provider.failQuery = false; provider.failInsert = true
        assertTrue(registration(provider).register(schedule(), target).contains("등록 실패"))
        assertEquals(0, provider.inserts)
        provider.failInsert = false; provider.lostResponse = true
        assertTrue(registration(provider).register(schedule(), target).contains("등록 실패"))
        provider.lostResponse = false
        assertTrue(registration(provider).register(schedule(), target).contains("등록됨"))
        assertEquals(1, provider.inserts)
    }

    @Test fun anotherThreadNeverReusesExistingRow() {
        val provider = Provider()
        registration(provider).register(schedule("thread-1"), target)
        registration(provider).register(schedule("thread-2"), target)
        assertEquals(2, provider.inserts)
        assertEquals(2, journal().mappings().map { it.rowId }.distinct().size)
    }

    @Test fun removedCalendarTargetNeedsANewSelectionAndSendsNothing() {
        val provider = Provider()
        provider.targets = emptyList()
        assertEquals("캘린더를 다시 선택하세요", registration(provider).register(schedule(), target))
        assertEquals(0, provider.inserts)
    }

    @Test fun copiedMarkerAndMultipleMatchesRequireManualConfirmation() {
        val provider = Provider().apply { failQuery = true }
        registration(provider).register(schedule(), target)
        val pending = journal().mappings().single()
        val event = pending.event!!
        provider.failQuery = false
        for (changed in listOf(event.copy(title = "Copied title"), event.copy(start = event.start + 1),
            event.copy(end = event.end + 1), event.copy(timezone = "America/New_York"))) {
            provider.events[pending.marker] = listOf(CalendarRow("content://com.android.calendar/events/999", 999, target.id, changed))
            assertTrue(registration(provider).register(schedule(), target).contains("수동 확인"))
            assertNull(journal().mappings().single().uri)
        }
        val row = CalendarRow("content://com.android.calendar/events/999", 999, target.id, event)
        provider.events[pending.marker] = listOf(row, row.copy(id = 1000))
        assertTrue(registration(provider).register(schedule(), target).contains("수동 확인"))
        provider.events[pending.marker] = listOf(row.copy(calendarId = 99))
        assertTrue(registration(provider).register(schedule(), target).contains("수동 확인"))
        provider.events.clear(); journal().put(pending.copy(event = null))
        assertTrue(registration(provider).register(schedule(), target).contains("수동 확인"))
        assertEquals(0, provider.inserts)
    }

    @Test fun seoulMidnightAndDstOffsetsRemainTimedWithOneHourFallback() {
        for ((start, end, zone) in listOf(
            Triple("2026-11-08T00:00:00+09:00", null, "Asia/Seoul"),
            Triple("2026-03-08T01:30:00-05:00", "2026-03-08T03:30:00-04:00", "America/New_York"),
            Triple("2026-11-01T01:30:00-04:00", "2026-11-01T01:30:00-05:00", "America/New_York"),
        )) {
            val event = CalendarEvent.from(schedule(start = start, end = end, zone = zone))
            assertEquals(OffsetDateTime.parse(start).toInstant().toEpochMilli(), event.start)
            assertEquals(3_600_000L, event.end - event.start)
            assertEquals(zone, event.timezone)
        }
        val event = CalendarEvent.from(schedule(end = "2026-01-01T10:30:00+09:00"))
        assertEquals(Instant.parse("2026-01-01T01:30:00Z").toEpochMilli(), event.end)
    }

    @Test fun journalsWithKeysFromOlderRegistrationsStillLoad() {
        File(temporary.root, "registrations.json").writeText("""{"mappings":[{"proposalId":"v2","rev":1,"ref":"thread-1",
            "calendar":{"id":42,"name":"Synthetic","account":"test@example.invalid","type":"LOCAL"},"marker":"m","uri":"content://x/1","rowId":1}]}""")
        val provider = Provider()
        assertTrue(registration(provider).register(schedule(), target).contains("등록됨"))
        assertEquals(0, provider.inserts)
    }
}
