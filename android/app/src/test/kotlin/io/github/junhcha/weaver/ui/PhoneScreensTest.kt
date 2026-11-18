package io.github.junhcha.weaver.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.v2.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import io.github.junhcha.weaver.phone.PhoneReadModel
import io.github.junhcha.weaver.phone.StatusCounts
import io.github.junhcha.weaver.phone.SyntheticPhoneReadModel
import java.time.Clock
import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneId
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36], qualifiers = "w411dp-h1600dp-xhdpi")
class PhoneScreensTest {
    @get:Rule val compose = createComposeRule()

    private val now: Instant = OffsetDateTime.parse("2026-11-18T09:00:00+09:00").toInstant()
    private val model: PhoneReadModel = SyntheticPhoneReadModel(Clock.fixed(now, ZoneId.of("UTC")))
    private var mapOpened = 0
    private var settingsOpened = 0

    private fun show(counts: StatusCounts = StatusCounts(2, 1, 0)) = compose.setContent {
        MaterialTheme { WeaverTheme { PhoneShell(model, counts, { now }, { mapOpened++ }, { settingsOpened++ }) } }
    }

    @Test fun nowShowsOnlyOverdueTodayAndNewThreadsWithTheStatusStrip() {
        show()
        compose.onNodeWithText("기한 지남").assertIsDisplayed()
        compose.onNodeWithText("협력사 A 견적서 회신").assertIsDisplayed()
        compose.onNodeWithText("마감 11/17 · 받은 요청 · 이서준").assertIsDisplayed()
        compose.onNodeWithText("오늘").assertIsDisplayed()
        compose.onNodeWithText("15:00 · 일정 · 네트워크").assertIsDisplayed()
        compose.onNodeWithText("마감 오늘 · 받은 요청 · 한지우").assertIsDisplayed()
        compose.onNodeWithText("새로 반영됨").assertIsDisplayed()
        compose.onNodeWithText("VPN 장비는 2주 뒤 교체로 확정").assertIsDisplayed()
        compose.onNodeWithText("11/18 (수)").assertIsDisplayed()
        // Not due yet, finished, or old: none of these belong on 지금.
        compose.onNodeWithText("방화벽 룰 3건 반영 요청").assertDoesNotExist()
        compose.onNodeWithText("VPN 계정 만료 연장").assertDoesNotExist()
        compose.onNodeWithText("UPS 점검 일정 정리").assertDoesNotExist()
    }

    @Test fun statusStripOpensTheMapAndSettingsButtonOpensSettings() {
        show()
        compose.onNodeWithText("검토 대기").assertIsDisplayed()
        compose.onNodeWithText("처리 중").assertIsDisplayed()
        compose.onNodeWithText("중지").assertIsDisplayed()
        compose.onNodeWithTag("status-strip").performClick()
        assertEquals(1, mapOpened)
        compose.onNodeWithText("수집·설정").performClick()
        assertEquals(1, settingsOpened)
    }

    @Test fun personTimelineHidesMentionsAndHistoryUntilToggled() {
        show()
        compose.onNodeWithTag("tab-PEOPLE").performClick()
        compose.onNodeWithText("이서준").performClick()
        compose.onNodeWithTag("timeline-screen").assertIsDisplayed()
        compose.onNodeWithText("장비 반입 시 이서준 동행 필요").assertDoesNotExist()
        compose.onNodeWithText("VPN 계정 만료일 연장 완료").assertDoesNotExist()

        compose.onNodeWithText("언급 포함").performClick()
        compose.onNodeWithText("장비 반입 시 이서준 동행 필요").assertExists()
        compose.onAllNodesWithText("언급").assertCountEquals(1)

        compose.onNodeWithText("이력 포함").performClick()
        compose.onNodeWithText("VPN 계정 만료일 연장 완료").performScrollTo().assertExists()
    }

    @Test fun areaTimelineOffersOnlyTheHistoryToggle() {
        show()
        compose.onNodeWithTag("tab-AREAS").performClick()
        compose.onNodeWithText("네트워크").performClick()
        compose.onNodeWithText("이력 포함").assertExists()
        compose.onNodeWithText("언급 포함").assertDoesNotExist()
    }

    @Test fun wikiViewerShowsFrontmatterAndLinksLeadToTimelines() {
        show()
        compose.onNodeWithTag("tab-WIKI").performClick()
        compose.onNodeWithText("방화벽 장비 교체").performClick()
        compose.onNodeWithTag("wiki-viewer").assertIsDisplayed()
        compose.onNodeWithText("읽기 전용").assertIsDisplayed()
        compose.onNodeWithTag("wiki-frontmatter").assertIsDisplayed()
        compose.onNodeWithText("진행 중").assertIsDisplayed()
        compose.onNodeWithText("마감").assertIsDisplayed()
        compose.onNodeWithText("11/28").assertIsDisplayed()
        compose.onNodeWithText("나, 이서준").assertIsDisplayed()
    }

    @Test fun wikiLinksOpenTheLinkedTimeline() {
        show()
        compose.onNodeWithTag("tab-WIKI").performClick()
        compose.onNodeWithText("방화벽 장비 교체").performClick()
        compose.onNodeWithText("나, 이서준").performClick() // clickable annotation on the assignee link
        compose.onNodeWithTag("timeline-screen").assertIsDisplayed()
    }
}
