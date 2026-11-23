package io.github.junhcha.weaver.ui

import androidx.activity.ComponentActivity
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import io.github.junhcha.weaver.phone.*
import java.time.Instant
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36], qualifiers = "w411dp-h1600dp-xhdpi")
class NowRecentTest {
    @get:Rule val compose = createAndroidComposeRule<ComponentActivity>()

    @Test fun recentHeadersUseOccurrenceAndAppendRecordedDayOnlyAcrossSeoulDates() {
        fun entry(id: String, occurred: String, recorded: String) = NowEntry(PhoneThread(
            id, 1, ThreadKind.RECORD, id, Instant.parse(occurred), Instant.parse(recorded), "통화", null, emptyList(),
        ))
        val view = NowView(emptyList(), emptyList(), listOf(
            entry("late", "2026-11-06T00:00:00Z", "2026-11-20T00:00:00Z"),
            entry("same-seoul-day", "2026-11-19T23:00:00Z", "2026-11-20T01:00:00Z"),
            entry("different-seoul-day", "2026-11-19T14:00:00Z", "2026-11-19T16:00:00Z"),
        ))
        compose.setContent { WeaverTheme {
            NowScreen(view, StatusCounts(0, 0, 0), Instant.parse("2026-11-20T00:00:00Z"), true, {})
        } }
        compose.onNodeWithText("11/6 09:00 · 기록 · 통화 · 11/20 반영").assertExists()
        compose.onNodeWithText("오늘 08:00 · 기록 · 통화").assertExists()
        compose.onNodeWithText("어제 23:00 · 기록 · 통화 · 11/20 반영").assertExists()
    }

    @Test fun revisedEntriesGetMarkerAndCardsFollowZone() {
        fun thread(id: String, occurred: String) = PhoneThread(
            id, 1, ThreadKind.RECORD, id, Instant.parse(occurred), Instant.parse(occurred), "통화", null, emptyList(),
        )
        val view = NowView(emptyList(), emptyList(), listOf(
            NowEntry(thread("revised", "2026-11-06T00:00:00Z"), revised = true),
            NowEntry(thread("zoned", "2026-11-19T15:30:00Z")),
        ))
        compose.setContent { WeaverTheme {
            NowScreen(view, StatusCounts(0, 0, 0), Instant.parse("2026-11-20T00:00:00Z"), true, {}, zone = java.time.ZoneId.of("UTC"))
        } }
        compose.onNodeWithText("11/6 00:00 · 기록 · 통화 · 변경 반영").assertExists()
        compose.onNodeWithText("어제 15:30 · 기록 · 통화").assertExists()
    }
}
