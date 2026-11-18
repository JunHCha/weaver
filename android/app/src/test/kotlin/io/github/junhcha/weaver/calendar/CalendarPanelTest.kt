package io.github.junhcha.weaver.calendar

import androidx.activity.compose.setContent
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import io.github.junhcha.weaver.MainActivity
import io.github.junhcha.weaver.schema.ActionKind
import io.github.junhcha.weaver.ui.CalendarActionRow
import io.github.junhcha.weaver.ui.actionStatus
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
    private val pending = ActionState("act_" + "0".repeat(32), ActionKind.REGISTER, "회의")

    @Test fun rowsShowTheStateWithoutAnyRegistrationButton() {
        compose.activity.setContent { CalendarActionRow(pending, true) }
        compose.onNodeWithText("등록 · 회의").assertExists()
        compose.onNodeWithText("대기 중").assertExists()
        compose.onNodeWithText("캘린더에 등록").assertDoesNotExist()
        compose.onAllNodes(hasClickAction()).assertCountEquals(0)
    }

    @Test fun statusesExplainWhatTheUserCanDo() {
        assertEquals("대기 중 · 대상 캘린더를 선택하세요", actionStatus(pending, false))
        assertEquals("대기 중 · 대상 캘린더를 선택하세요", actionStatus(pending.copy(note = "target"), true))
        assertEquals("대기 중 · 캘린더 권한이 필요합니다", actionStatus(pending.copy(note = "permission"), true))
        assertEquals("재시도 대기 · 2회 실패", actionStatus(pending.copy(phase = ActionPhase.RETRY, attempts = 2), true))
        assertEquals("완료", actionStatus(pending.copy(phase = ActionPhase.DONE), true))
        assertEquals("포기 · 수동 확인 필요: 캘린더의 기존 일정을 확인하세요",
            actionStatus(pending.copy(phase = ActionPhase.FAILED, error = "manual_review"), true))
        assertEquals("포기 · 5회 시도했지만 실패했습니다",
            actionStatus(pending.copy(phase = ActionPhase.FAILED, attempts = 5, error = "provider_error"), true))
    }

    private fun putAction() {
        val file = java.io.File(io.github.junhcha.weaver.storage.AppStorage.exchangeDir(compose.activity), "calendar/registrations.json")
        CalendarJournal(file).put(pending)
    }

    @Test fun settingsShowsTheCalendarPanelOnceEvenWithAnAction() {
        putAction()
        compose.activity.setContent { io.github.junhcha.weaver.ui.WeaverTheme { io.github.junhcha.weaver.ui.ToolsScreen() } }
        compose.waitUntil(5_000) { compose.onAllNodesWithText("등록 · 회의").fetchSemanticsNodes().isNotEmpty() }
        compose.onAllNodesWithText("승인 일정 · 자동 반영").assertCountEquals(1)
        compose.onAllNodesWithText("캘린더 선택 / 권한 요청").assertCountEquals(1)
        compose.onAllNodesWithText("등록 · 회의").assertCountEquals(1)
    }

    @Test fun reviewSlotAddsNoSecondPanelInsideSettingsButStillShowsOutside() {
        putAction()
        compose.activity.setContent {
            androidx.compose.foundation.layout.Column {
                androidx.compose.runtime.CompositionLocalProvider(io.github.junhcha.weaver.ui.LocalCalendarHostedBySettings provides true) {
                    io.github.junhcha.weaver.ui.ReviewCalendarSlot()
                }
            }
        }
        compose.waitForIdle()
        Thread.sleep(300)
        compose.onAllNodesWithText("승인 일정 · 자동 반영").assertCountEquals(0)
        compose.activity.setContent { io.github.junhcha.weaver.ui.ReviewCalendarSlot() }
        compose.waitUntil(5_000) { compose.onAllNodesWithText("승인 일정 · 자동 반영").fetchSemanticsNodes().isNotEmpty() }
        compose.onAllNodesWithText("승인 일정 · 자동 반영").assertCountEquals(1)
    }
}
