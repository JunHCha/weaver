package io.github.junhcha.weaver.calendar

import androidx.activity.compose.setContent
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import io.github.junhcha.weaver.MainActivity
import io.github.junhcha.weaver.schema.ApprovedSchedule
import io.github.junhcha.weaver.ui.CalendarSchedule
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class CalendarPanelTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()
    private val schedule = ApprovedSchedule(2, "package", "thread", "회의", "2026-01-01T09:00:00+09:00", null, "Asia/Seoul", null, "a".repeat(64))

    @Test fun registrationRequiresButtonAndBlockedScheduleHasNoButton() {
        var registrations = 0
        compose.activity.setContent { CalendarSchedule(schedule, true) { registrations++ } }
        compose.onNodeWithText("캘린더에 등록").assertIsEnabled()
        assertEquals(0, registrations)
        compose.onNodeWithText("캘린더에 등록").performClick()
        assertEquals(1, registrations)
        compose.activity.setContent { CalendarSchedule(schedule.copy(blockedBy = "request"), true) { registrations++ } }
        compose.onNodeWithText("요청 request 미해소 · 등록 차단").assertExists()
        compose.onNodeWithText("캘린더에 등록").assertDoesNotExist()
        assertEquals(1, registrations)
    }
}
