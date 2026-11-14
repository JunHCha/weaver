package io.github.junhcha.weaver.pipeline

import androidx.activity.ComponentActivity
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.*
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import io.github.junhcha.weaver.schema.*
import io.github.junhcha.weaver.ui.PipelineContent
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk=[36],qualifiers="w1200dp-h800dp-land")
class PipelineScreenTest {
    @get:Rule val compose=createAndroidComposeRule<ComponentActivity>()
    private val review=MapItem("cap-1","가상 승인 항목","통화",MapNode.USER,"review",mapOf(Stage.REVIEW to "2026-11-12T01:00:00Z"),"package-1",1)
    @Test fun mapKeepsSixNodeTargetsAndUserPanelContainsOnlyApprovalCards() {
        compose.setContent {
            var selected by remember { mutableStateOf<MapNode?>(null) }
            MaterialTheme { PipelineContent(PipelineScreenState(listOf(review,review.copy(id="cap-2",title="가상 처리 중",node=MapNode.SPINNER,status="working")),loaded=true),selected,{selected=it},0,{}, {}) }
        }
        for(node in MapNode.entries.filter { it!=MapNode.COURIER }) compose.onNodeWithContentDescription("${node.label} 노드").assertExists()
        compose.onNodeWithContentDescription("You 노드").performClick()
        compose.onNodeWithText("엮은 내용을 확인하고 승인해 주세요").assertIsDisplayed()
        compose.onNodeWithText("가상 처리 중").assertDoesNotExist()
        compose.onNodeWithText("가상 승인 항목").performClick()
        compose.onNodeWithText("승인하러 가기").performScrollTo().assertIsEnabled()
        compose.onNodeWithText("승인 후 시작").assertExists()
    }
    @Test @Config(qualifiers="w400dp-h800dp-port")
    fun portraitNodeOpensDismissibleSheet() {
        compose.setContent {
            var selected by remember { mutableStateOf<MapNode?>(null) }
            MaterialTheme { PipelineContent(PipelineScreenState(listOf(review),loaded=true),selected,{selected=it},0,{}, {}) }
        }
        compose.onNodeWithContentDescription("You 노드").performClick()
        compose.onNodeWithText("엮은 내용을 확인하고 승인해 주세요").assertIsDisplayed()
        compose.onNodeWithText("닫기").performClick()
        compose.onNodeWithText("엮은 내용을 확인하고 승인해 주세요").assertDoesNotExist()
        compose.onNodeWithContentDescription("Shepherd 노드").assertIsDisplayed()
    }
    @Test fun wikiPageWithoutPhoneVaultIsDisabled() {
        val page=PipelinePage("wiki/가상 페이지.md",java.time.OffsetDateTime.now().toString(),"a".repeat(64))
        val item=PipelineItem(2,1,"cap_01ARZ3NDEKTSV4RRFFQ69G5FAV","src_01ARZ3NDEKTSV4RRFFQ69G5FAV",null,null,"share_text","가상 결과",PipelineLocation.WIKI,PipelineStatus.DONE,emptyMap(),listOf(PipelineLocation.WIKI),emptyList(),listOf(page),null,5,page.updatedAt)
        compose.setContent { MaterialTheme { PipelineContent(PipelineScreenState(listOf(review.copy(id=checkNotNull(item.captureId),node=MapNode.WIKI,status="done",server=item)),loaded=true),MapNode.WIKI,{},0,{}, {}) } }
        compose.onNodeWithText("가상 페이지 · 폰에 오는 중").assertExists().assertIsNotEnabled()
        compose.onNodeWithText("폰에 동기화한 vault 폴더 선택").performScrollTo().assertIsDisplayed()
    }
    @Test fun wikiEditBlockOpensChangesThreadsAndDisabledOutputUntilPhoneArrival() {
        val server = WeaverCodec.json.decodeFromString<PipelineItem>(java.io.File(System.getProperty("weaver.schemaDir"), "examples/PipelineItem-wiki-edit.json").readText())
        val event = MapItem(server.sourceId, server.title, "Wiki 직접 수정", MapNode.WIKI, "done", server.stageTimes, server = server)
        compose.setContent {
            var selected by remember { mutableStateOf<MapNode?>(null) }
            MaterialTheme { PipelineContent(PipelineScreenState(listOf(event), loaded = true), selected, { selected = it }, 0, {}, {}) }
        }
        compose.onNodeWithContentDescription("${event.title}: ${event.stateLabel}").performClick()
        compose.onNodeWithText("Wiki에서 시작 · 사람 직접 수정").assertIsDisplayed()
        compose.onNodeWithText("마감 변경").assertExists()
        compose.onNodeWithText("2026-11-16").assertExists()
        compose.onNodeWithText("2026-11-19").assertExists()
        compose.onNodeWithText("Thread 1").performClick()
        compose.onNodeWithText("Thread · Record").assertExists()
        compose.onNodeWithText("마감을 11월 19일로 변경").assertExists()
        compose.onNodeWithText("결과물").performClick()
        compose.onNodeWithText("보안장비 점검 · 폰에 오는 중").assertIsNotEnabled()
    }

}
