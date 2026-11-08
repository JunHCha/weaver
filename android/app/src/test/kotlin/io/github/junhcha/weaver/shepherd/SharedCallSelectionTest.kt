package io.github.junhcha.weaver.shepherd

import io.github.junhcha.weaver.schema.Channel
import org.junit.Assert.*
import org.junit.Test

class SharedCallSelectionTest {
    private val name = "통화 가짜 상대_261108_120030_original.txt"
    private val match = PendingCall("match", "2026-11-08T12:00:25+09:00", "가짜 상대")
    private val latest = PendingCall("latest", "2026-11-08T13:00:00+09:00", "가짜 상대")

    @Test fun matchesCounterpartAndMinuteBeforeMostRecentCall() {
        assertEquals("match", SharedCallSelection.call(name, listOf(latest, match)))
        assertEquals("match", SharedCallSelection.call(name, listOf(match, latest)))
        assertEquals("match", SharedCallSelection.call(name, listOf(latest,
            match.copy(occurredAt = "2026-11-08T12:00:59+09:00"))))
    }
    @Test fun fallsBackToMostRecentForMismatchOrInvalidFilename() {
        listOf("통화 다른 상대_261108_120030_original.txt", "통화 가짜 상대_261108_115959_original.txt",
            "통화 가짜 상대_261132_120030_original.txt", "../$name", "ordinary.txt", null).forEach {
            assertEquals("latest", SharedCallSelection.call(it, listOf(match, latest)))
        }
        assertNull(SharedCallSelection.call(name, emptyList()))
    }
    @Test fun usesRecordingNameWhenMetadataIsUnknown() {
        val recording = PendingCall("recording", null, null, "가짜 상대_261108_120031.m4a")
        assertEquals("recording", SharedCallSelection.call(name, listOf(latest, recording)))
        val long = recording.copy(displayName = "Call recording 가짜 상대_20261108_120031.m4a")
        assertEquals("recording", SharedCallSelection.call(name, listOf(latest, long)))
    }
    @Test fun multipleMatchesPreferNearestSecondThenLatestInstantThenId() {
        val near = match.copy(id = "near", occurredAt = "2026-11-08T12:00:31+09:00")
        assertEquals("near", SharedCallSelection.call(name, listOf(latest, match, near)))
        val tie = near.copy(id = "tie", occurredAt = "2026-11-08T12:00:29+09:00")
        assertEquals("near", SharedCallSelection.call(name, listOf(tie, near)))
        assertEquals("a", SharedCallSelection.call(name, listOf(near.copy(id = "z"), near.copy(id = "a"))))
    }
    @Test fun defaultsToCallOnlyForValidTranscriptAndAvailableCalls() {
        assertEquals(Channel.CALL, SharedCallSelection.channel(name, listOf(latest)))
        assertEquals(Channel.SHARE_TEXT, SharedCallSelection.channel(name, emptyList()))
        assertEquals(Channel.SHARE_TEXT, SharedCallSelection.channel("ordinary.txt", listOf(match)))
        assertEquals(Channel.SHARE_TEXT, SharedCallSelection.channel(null, listOf(match)))
        assertEquals(Channel.SHARE_TEXT, SharedCallSelection.channel("통화 가짜_261132_120000_original.txt", listOf(match)))
    }
}
