package io.github.junhcha.weaver.ui

import androidx.activity.ComponentActivity
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.width
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
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
        compose.onNodeWithText("상대 민수").assertIsNotEnabled()
        compose.onNodeWithText("#${card.relatedTags.single().name}").assertIsNotEnabled()
        compose.onNodeWithText("출처 공유 텍스트").assertIsDisplayed()
    }

    @Test fun oldCardHasNoInventedSummaryTagsOrCounterpart() {
        val card = item().copy(summary = "", counterpart = null, relatedTags = emptyList())
        compose.setContent { WeaverTheme { Column { FabricCardInfo(card) } } }
        compose.onNodeWithText("상대 미상").assertIsDisplayed()
        compose.onNodeWithText("출처 공유 텍스트").assertIsDisplayed()
        compose.onNodeWithText("#검색").assertDoesNotExist()
    }

    @Test fun subjectResolutionRequiresMatchingPhoneDirectoryKind() {
        val snapshot = PhoneSnapshot(emptyList(), listOf(PhoneTag("민수", TagKind.PERSON), PhoneTag("검색", TagKind.AREA)), emptyList())
        assertEquals(LinkTarget.Person("민수"), fabricSubjectTarget(snapshot, "민수", "person"))
        assertEquals(LinkTarget.Area("검색"), fabricSubjectTarget(snapshot, "검색", "area"))
        assertEquals(LinkTarget.None, fabricSubjectTarget(snapshot, "검색", "person"))
        assertEquals(LinkTarget.None, fabricSubjectTarget(snapshot, "없는사람", "person"))
        assertEquals(LinkTarget.None, fabricSubjectTarget(null, "민수", "person"))
        assertEquals(LinkTarget.None, fabricSubjectTarget(snapshot, "검색", null))
    }
}
