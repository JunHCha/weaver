package io.github.junhcha.weaver.ui

import androidx.activity.ComponentActivity
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.width
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.semantics.getOrNull
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.phone.*
import io.github.junhcha.weaver.schema.*
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36], qualifiers = "w411dp-h1600dp-xhdpi")
class FabricCardInfoTest {
    @get:Rule val compose = createAndroidComposeRule<ComponentActivity>()
    private fun item() = WeaverCodec.json.decodeFromString<PipelineItem>(
        java.io.File(System.getProperty("weaver.schemaDir"), "examples/PipelineItem-card.json").readText(),
    )

    @Test fun narrowCardShowsOneLineSummaryAndDisablesUnknownSubjects() {
        val summary = "검색 시안과 다음 검토 일정을 확인한다. ".repeat(20)
        val card = item().copy(summary = summary, relatedTags = listOf(PipelineTag("아주 긴 미분류 태그 ".repeat(15))))
        compose.setContent { WeaverTheme { Column(Modifier.width(280.dp)) { FabricCardInfo(card) } } }
        compose.onNodeWithText(summary.trim()).performSemanticsAction(androidx.compose.ui.semantics.SemanticsActions.GetTextLayoutResult) { action ->
            val result = mutableListOf<androidx.compose.ui.text.TextLayoutResult>()
            action(result)
            assertEquals(1, result.single().lineCount)
            assertTrue(result.single().hasVisualOverflow)
        }
        compose.onNodeWithText("상대 민수").assertHasNoClickAction()
        compose.onNodeWithText("#${card.relatedTags.single().name}").assertHasNoClickAction()
        compose.onNodeWithText("출처 공유 텍스트").assertIsDisplayed()
    }

    @Test fun oldCardHasNoInventedSummaryTagsOrCounterpart() {
        val card = item().copy(summary = "", counterpart = null, relatedTags = emptyList())
        compose.setContent { WeaverTheme { Column { FabricCardInfo(card) } } }
        compose.onNodeWithText("상대 미상").assertDoesNotExist()
        compose.onNodeWithText("상대", substring = true).assertDoesNotExist()
        compose.onNodeWithText("출처 공유 텍스트").assertIsDisplayed()
        compose.onNodeWithText("#검색").assertDoesNotExist()
    }

    @Test fun wikiEditCardNeverShowsCounterpartLine() {
        val card = item().copy(counterpart = "민수")
        compose.setContent { WeaverTheme { Column { FabricCardInfo(card, showCounterpart = false) } } }
        compose.onNodeWithText("상대 민수").assertDoesNotExist()
    }

    @Test fun navigableSubjectsAreLabelledClickTargetsAndOthersAreNot() {
        val snapshot = PhoneSnapshot(emptyList(), listOf(PhoneTag("민수", TagKind.PERSON), PhoneTag("검색", TagKind.AREA), PhoneTag("제품팀", TagKind.ORGANIZATION)), emptyList())
        val opened = mutableListOf<LinkTarget>()
        val card = item().copy(counterpart = "민수", relatedTags = listOf(PipelineTag("검색", "area"), PipelineTag("제품팀", "organization"), PipelineTag("주제어", "topic"), PipelineTag("없음")))
        compose.setContent { WeaverTheme { Column { FabricCardInfo(card, { n, k -> fabricSubjectTarget(snapshot, n, k) }, { opened += it }) } } }
        fun label(text: String) = SemanticsMatcher("onClickLabel") { it.config.getOrNull(androidx.compose.ui.semantics.SemanticsActions.OnClick)?.label == text }
        compose.onNodeWithText("상대 민수").assert(label("사람 타임라인 열기")).performClick()
        compose.onNodeWithText("#검색").assert(label("영역 타임라인 열기")).performClick()
        compose.onNodeWithText("#제품팀").assert(label("사람 타임라인 열기")).performClick()
        compose.onNodeWithText("#주제어").assertHasNoClickAction()
        compose.onNodeWithText("#없음").assertHasNoClickAction()
        compose.onNodeWithContentDescription("영역 검색").assertExists()
        assertEquals(listOf(LinkTarget.Person("민수"), LinkTarget.Area("검색"), LinkTarget.Person("제품팀")), opened)
    }

    @Test fun narrowWidthEllipsizesLongCounterpartAndSourceLines() {
        val card = item().copy(counterpart = "아주 긴 상대 이름 ".repeat(20), captureHosts = listOf(CaptureHost.PHONE, CaptureHost.MAC, CaptureHost.DEVICE))
        compose.setContent { WeaverTheme { Column(Modifier.width(120.dp)) { FabricCardInfo(card) } } }
        for (matcher in listOf(hasText("상대", substring = true), hasText("출처", substring = true))) {
            compose.onNode(matcher).performSemanticsAction(androidx.compose.ui.semantics.SemanticsActions.GetTextLayoutResult) { action ->
                val result = mutableListOf<androidx.compose.ui.text.TextLayoutResult>()
                action(result)
                assertEquals(1, result.single().lineCount)
                assertTrue(result.single().hasVisualOverflow)
            }
        }
    }

    @Test fun mapOnlyBackReturnsForTheSubjectOpenedFromTheMap() {
        var back = 0
        val model = SyntheticPhoneReadModel()
        compose.setContent {
            WeaverTheme { PhoneShell(model, StatusCounts(0, 0, 0), { java.time.Instant.now() }, {}, {}, initialTab = PhoneTab.PEOPLE, initialSubject = "이서준", onSubjectBack = { back++ }) }
        }
        compose.waitUntil(5_000) { compose.onAllNodesWithTag("timeline-screen").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithContentDescription("뒤로").performClick()
        assertEquals(1, back)
    }

    @Test fun otherSubjectsKeepNormalBackBehaviorEvenWhenAMapSubjectWasPassed() {
        var back = 0
        val model = SyntheticPhoneReadModel()
        compose.setContent {
            WeaverTheme { PhoneShell(model, StatusCounts(0, 0, 0), { java.time.Instant.now() }, {}, {}, initialTab = PhoneTab.NOW, initialSubject = "이서준", onSubjectBack = { back++ }) }
        }
        compose.onNodeWithTag("tab-PEOPLE").performClick()
        compose.onNodeWithText("한지우").performClick()
        compose.onNodeWithTag("timeline-screen").assertIsDisplayed()
        compose.onNodeWithContentDescription("뒤로").performClick()
        assertEquals(0, back)
        compose.onNodeWithText("이서준").assertIsDisplayed()
    }

    @Test fun subjectResolutionRequiresMatchingPhoneDirectoryKind() {
        val snapshot = PhoneSnapshot(emptyList(), listOf(PhoneTag("민수", TagKind.PERSON), PhoneTag("검색", TagKind.AREA)), emptyList())
        assertEquals(LinkTarget.Person("민수"), fabricSubjectTarget(snapshot, "민수", "person"))
        assertEquals(LinkTarget.Area("검색"), fabricSubjectTarget(snapshot, "검색", "area"))
        assertEquals(LinkTarget.None, fabricSubjectTarget(snapshot, "검색", "person"))
        assertEquals(LinkTarget.None, fabricSubjectTarget(snapshot, "없는사람", "person"))
        assertEquals(LinkTarget.None, fabricSubjectTarget(null, "민수", "person"))
        assertEquals(LinkTarget.None, fabricSubjectTarget(snapshot, "검색", null))
        // Same name in two kinds resolves by name and kind together, in either directory order.
        for (tags in listOf(listOf(PhoneTag("검색", TagKind.PERSON), PhoneTag("검색", TagKind.AREA)), listOf(PhoneTag("검색", TagKind.AREA), PhoneTag("검색", TagKind.PERSON)))) {
            val both = PhoneSnapshot(emptyList(), tags, emptyList())
            assertEquals(LinkTarget.Person("검색"), fabricSubjectTarget(both, "검색", "person"))
            assertEquals(LinkTarget.Area("검색"), fabricSubjectTarget(both, "검색", "area"))
        }
        // Organizations live in the people tab.
        val org = PhoneSnapshot(emptyList(), listOf(PhoneTag("제품팀", TagKind.ORGANIZATION)), emptyList())
        assertEquals(LinkTarget.Person("제품팀"), fabricSubjectTarget(org, "제품팀", "organization"))
        // Composed (NFC) and decomposed (NFD) Hangul compare equal; the directory's own spelling is used.
        val nfd = java.text.Normalizer.normalize("민수", java.text.Normalizer.Form.NFD)
        val decomposed = PhoneSnapshot(emptyList(), listOf(PhoneTag(nfd, TagKind.PERSON)), emptyList())
        assertEquals(LinkTarget.Person(nfd), fabricSubjectTarget(decomposed, "민수", "person"))
    }
}
