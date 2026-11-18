package io.github.junhcha.weaver.ui

import android.graphics.Bitmap
import androidx.activity.ComponentActivity
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import io.github.junhcha.weaver.pipeline.*
import io.github.junhcha.weaver.schema.*
import java.io.File
import java.time.OffsetDateTime
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

/** Synthetic map panels; optionally retain PNG evidence with WEAVER_SCREENSHOT_DIR. */
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [36], qualifiers = "w1200dp-h800dp-land-xhdpi")
class MapPanelScreenshotTest {
    @get:Rule val compose = createAndroidComposeRule<ComponentActivity>()
    private val updatedAt = OffsetDateTime.now().toString()
    private val item = PipelineItem(
        2, 1, "cap_01ARZ3NDEKTSV4RRFFQ69G5FAV", "src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        null, null, "call", "김민수 · 장비 점검 통화", PipelineLocation.FABRIC, PipelineStatus.DONE,
        mapOf(Stage.APPLY to updatedAt), listOf(PipelineLocation.FABRIC),
        listOf(
            PipelineThread("thread-1", "요청", "금요일까지 장비 점검 결과를 공유해 주세요. 추가 설명은 제목에 표시하지 않습니다."),
            PipelineThread("thread-2", "결정", "다음 주 월요일에 방화벽을 교체하기로 했습니다."),
            PipelineThread("thread-3", "기록", "회의에서 확인한 장비 점검 항목과 후속 조치 내용을 담당자에게 전달하고 다음 회의 전에 진행 상황을 함께 확인하기로 정리했습니다."),
        ),
        listOf(PipelinePage("pages/방화벽 장비 교체.md", updatedAt, "a".repeat(64))),
        null, 5, updatedAt,
    )

    private fun panel(node: MapNode) {
        val mapped = MapItem(checkNotNull(item.captureId), item.title, "통화", node, "done", item.stageTimes, server = item)
        compose.setContent { WeaverTheme { PipelineContent(PipelineScreenState(listOf(mapped), loaded = true), node, {}, 0, {}, {}) } }
        compose.waitForIdle()
    }

    private fun shoot(name: String) {
        val path = System.getenv("WEAVER_SCREENSHOT_DIR") ?: return
        val dir = File(path).apply { mkdirs() }
        val view = compose.activity.window.decorView
        val bitmap = Bitmap.createBitmap(view.width, view.height, Bitmap.Config.ARGB_8888)
        view.draw(android.graphics.Canvas(bitmap))
        File(dir, "$name.png").outputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) }
    }

    @Test fun fabricPanelShowsContentAndSourceMetadata() {
        panel(MapNode.FABRIC)
        compose.onNodeWithText("금요일까지 장비 점검 결과를 공유해 주세요.").assertIsDisplayed()
        compose.onNodeWithText("Thread · Request").assertDoesNotExist()
        compose.onNodeWithText("요청 · ${item.title} · 반영 ${mapTime(updatedAt)}").assertExists()
        compose.onNodeWithText(item.threads.last().content).performSemanticsAction(
            androidx.compose.ui.semantics.SemanticsActions.GetTextLayoutResult,
        ) { action ->
            val results = mutableListOf<androidx.compose.ui.text.TextLayoutResult>()
            action(results)
            org.junit.Assert.assertEquals(2, results.single().lineCount)
            org.junit.Assert.assertTrue(results.single().hasVisualOverflow)
        }
        shoot("map-fabric-panel")
    }

    @Test fun wikiPanelShowsPageNameAndUpdateMetadata() {
        panel(MapNode.WIKI)
        compose.onNodeWithText("방화벽 장비 교체 · 폰에 오는 중").assertIsNotEnabled()
        compose.onNodeWithText("${mapTime(updatedAt)} · Wiki 페이지").assertExists()
        compose.onNodeWithText("Thread · Request").assertDoesNotExist()
        shoot("map-wiki-panel")
    }
}
