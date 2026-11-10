package io.github.junhcha.weaver.calendar

import androidx.activity.compose.setContent
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import io.github.junhcha.weaver.MainActivity
import io.github.junhcha.weaver.schema.v2.ApprovedSchedule
import io.github.junhcha.weaver.ui.CalendarV2Schedule
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class CalendarV2PanelTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()
    private val schedule = ApprovedSchedule(2, "package", "thread", "회의", "2026-01-01T09:00:00+09:00", null, "Asia/Seoul", null, "a".repeat(64))

    @Test fun registrationRequiresButtonAndBlockedScheduleHasNoButton() {
        var registrations = 0
        compose.activity.setContent { CalendarV2Schedule(schedule, true) { registrations++ } }
        compose.onNodeWithText("캘린더에 등록").assertIsEnabled()
        assertEquals(0, registrations)
        compose.onNodeWithText("캘린더에 등록").performClick()
        assertEquals(1, registrations)
        compose.activity.setContent { CalendarV2Schedule(schedule.copy(blockedBy = "request"), true) { registrations++ } }
        compose.onNodeWithText("요청 request 미해소 · 등록 차단").assertExists()
        compose.onNodeWithText("캘린더에 등록").assertDoesNotExist()
        assertEquals(1, registrations)
    }
}
