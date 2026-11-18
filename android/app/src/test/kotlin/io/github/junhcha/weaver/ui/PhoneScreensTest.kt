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
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.ui.test.assertHeightIsAtLeast
import androidx.compose.ui.test.assertWidthIsAtLeast
import androidx.compose.ui.test.assertIsOn
import androidx.compose.ui.test.assertIsOff
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.assert
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.test.core.app.ActivityScenario
import io.github.junhcha.weaver.phone.PhoneReadModels
import io.github.junhcha.weaver.phone.PhoneSnapshot
import org.junit.Assert.assertTrue
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

    private fun show(counts: StatusCounts = StatusCounts(2, 1, 0), readModel: PhoneReadModel = model) = compose.setContent {
        MaterialTheme { WeaverTheme { PhoneShell(readModel, counts, { now }, { mapOpened++ }, { settingsOpened++ }) } }
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

    // ── 합성 데이터는 실제처럼 보이면 안 된다 ─────────────────────────────────

    @Test fun exampleDataIsAlwaysBadgedOnEveryTab() {
        show()
        PhoneTab.entries.forEach { tab ->
            compose.onNodeWithTag("tab-${tab.name}").performClick()
            compose.onNodeWithTag("example-badge").assertIsDisplayed()
            compose.onNodeWithText("예시 데이터").assertIsDisplayed()
        }
    }

    @Test fun releaseWiringShowsNoConnectedDataAndNoSyntheticStrings() {
        val release = PhoneReadModels.forBuild(debug = false)
        show(readModel = release)
        val synthetic = kotlinx.coroutines.runBlocking { SyntheticPhoneReadModel(Clock.fixed(now, ZoneId.of("UTC"))).snapshot() }
        val forbidden = synthetic.threads.map { it.content } + synthetic.tags.map { it.name } + synthetic.wikiPages.map { it.title } +
            "예시 데이터"
        PhoneTab.entries.forEach { tab ->
            compose.onNodeWithTag("tab-${tab.name}").performClick()
            compose.onNodeWithText("연결된 데이터 없음").assertIsDisplayed()
            forbidden.forEach { compose.onNodeWithText(it).assertDoesNotExist() }
            compose.onNodeWithTag("example-badge").assertDoesNotExist()
        }
    }

    // ── 접근성 ───────────────────────────────────────────────────────────────

    @Test fun timelineChipsAreCheckboxesWithStateAndBigTargets() {
        show()
        compose.onNodeWithTag("tab-PEOPLE").performClick()
        compose.onNodeWithText("이서준").performClick()
        val chip = compose.onNode(androidx.compose.ui.test.hasText("언급 포함") and androidx.compose.ui.test.isToggleable())
        chip.assertIsOff()
        chip.performClick()
        chip.assertIsOn()
        chip.assert(androidx.compose.ui.test.SemanticsMatcher.expectValue(
            androidx.compose.ui.semantics.SemanticsProperties.Role, androidx.compose.ui.semantics.Role.Checkbox))
        chip.assertHeightIsAtLeast(48.dp)
        compose.onNodeWithTag("status-strip").assertDoesNotExist()
        compose.onNodeWithContentDescription("뒤로").assertHeightIsAtLeast(48.dp).assertWidthIsAtLeast(48.dp)
    }

    @Test fun statusStripAndBackButtonsMeetTheTouchTargetMinimum() {
        show()
        compose.onNodeWithTag("status-strip").assertHeightIsAtLeast(48.dp)
        compose.onNodeWithText("수집·설정").assertHeightIsAtLeast(48.dp)
    }

    @Test fun mutedMentionTextKeepsReadableContrast() {
        // Muted ink on the lightest (surface) and the tinted record card, per WCAG AA for body text.
        listOf(WeaverTokens.surface, kindBackground(WeaverTokens.record), kindBackground(WeaverTokens.request)).forEach {
            assertTrue(contrastRatio(WeaverTokens.muted, it) >= 4.5)
        }
        assertTrue(contrastRatio(WeaverTokens.indigo, WeaverTokens.bg) >= 4.5) // "기한 지남" heading
    }

    @Test fun overdueHeadingIsStrongerThanTheOtherSections() {
        show()
        compose.onNodeWithText("기한 지남").assertIsDisplayed()
    }

    // ── 재개 시 다시 읽기 ───────────────────────────────────────────────────

    @Test fun resumeReReadsTheSnapshot() {
        var reads = 0
        val counting = object : PhoneReadModel {
            override suspend fun snapshot(): PhoneSnapshot { reads++; return model.snapshot() }
        }
        val scenario = ActivityScenario.launch(ComponentActivity::class.java)
        scenario.onActivity { it.setContent { MaterialTheme { WeaverTheme { PhoneShell(counting, StatusCounts(0, 0, 0), { now }, {}, {}) } } } }
        compose.waitForIdle()
        val before = reads
        scenario.moveToState(Lifecycle.State.STARTED)
        scenario.moveToState(Lifecycle.State.RESUMED)
        compose.waitForIdle()
        assertTrue("snapshot re-read on resume ($before -> $reads)", reads > before)
        scenario.close()
    }
}
