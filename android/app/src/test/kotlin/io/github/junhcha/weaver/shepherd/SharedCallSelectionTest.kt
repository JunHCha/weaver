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
        listOf("통화 다른 상대_261108_120030_original.txt", "통화 가짜 상대_261108_115924_original.txt",
            "통화 가짜 상대_261108_120030_original (0).txt", "통화 가짜 상대_261108_120030_original (x).txt",
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
    @Test fun matchesWithinSixtySecondsAcrossMinuteBoundary() {
        val before = match.copy(id = "before", occurredAt = "2026-11-08T11:59:50+09:00")
        val edge = "통화 가짜 상대_261108_120010_original.txt"
        assertEquals("before", SharedCallSelection.call(edge, listOf(latest, before)))
        assertEquals("before", SharedCallSelection.call("통화 가짜 상대_261108_120050_original.txt",
            listOf(latest, before)))
        assertEquals("latest", SharedCallSelection.call("통화 가짜 상대_261108_120051_original.txt",
            listOf(latest, before)))
        val after = match.copy(id = "after", occurredAt = "2026-11-08T12:00:15+09:00")
        assertEquals("after", SharedCallSelection.call(edge, listOf(latest, before, after)))
    }
    @Test fun matchesPrefixedRecordingNames() {
        listOf("통화 가짜 상대_261108_120031.m4a", "통화 녹음 가짜 상대_261108_120031.m4a",
            "Call recording 가짜 상대_261108_120031.m4a").forEach {
            val recording = PendingCall("recording", null, null, it)
            assertEquals(it, "recording", SharedCallSelection.call(name, listOf(latest, recording)))
        }
        // Rows stored before #30 stripped prefixes still carry them.
        assertEquals("old", SharedCallSelection.call(name, listOf(latest,
            match.copy(id = "old", counterpart = "통화 가짜 상대"))))
        // Same base name matches even when metadata cannot be compared.
        val same = PendingCall("same", null, null, "통화 가짜 상대_261108_120030.M4A")
        assertEquals("same", SharedCallSelection.call(name, listOf(latest, same)))
    }
    @Test fun acceptsUnderscoreNamesNfdDuplicateSuffixAndExtensionCase() {
        val underscored = PendingCall("under", "2026-11-08T12:00:25+09:00", "가짜_상대")
        assertEquals("under", SharedCallSelection.call("통화 가짜_상대_261108_120030_original.txt",
            listOf(latest, underscored)))
        val nfd = java.text.Normalizer.normalize(name, java.text.Normalizer.Form.NFD)
        assertNotEquals(name, nfd)
        assertEquals("match", SharedCallSelection.call(nfd, listOf(latest, match)))
        val nfdCall = match.copy(id = "nfd", counterpart = java.text.Normalizer.normalize("가짜 상대",
            java.text.Normalizer.Form.NFD))
        assertEquals("nfd", SharedCallSelection.call(name, listOf(latest, nfdCall)))
        listOf("통화 가짜 상대_261108_120030_original (2).txt", "통화 가짜 상대_261108_120030_original.TXT",
            "통화 가짜 상대_261108_120030_original (12).Txt").forEach {
            assertEquals(it, "match", SharedCallSelection.call(it, listOf(latest, match)))
            assertEquals(Channel.CALL, SharedCallSelection.channel(it, listOf(latest)))
        }
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
