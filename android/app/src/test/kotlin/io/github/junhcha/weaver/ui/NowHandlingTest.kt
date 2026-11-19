package io.github.junhcha.weaver.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.v2.createComposeRule
import androidx.test.core.app.ApplicationProvider
import io.github.junhcha.weaver.phone.*
import io.github.junhcha.weaver.transport.ContractFixtures
import io.github.junhcha.weaver.transport.MobileApi
import java.lang.reflect.Proxy
import java.time.Instant
import java.util.UUID
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.*
import org.junit.After
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36], qualifiers = "w411dp-h1600dp-xhdpi")
class NowHandlingTest {
    @get:Rule val compose = createComposeRule()
    private val store = NowStore(ApplicationProvider.getApplicationContext(), "now-ui-${UUID.randomUUID()}.db")
    private val now = Instant.parse("2026-11-19T00:00:00Z")
    private var readOnly = false
    private var fail = false
    private val raw = ContractFixtures["request_handling.now_since_seq"].responseBody!!.jsonObject
    private val sent = mutableListOf<JsonObject>()
    private val api = Proxy.newProxyInstance(MobileApi::class.java.classLoader, arrayOf(MobileApi::class.java)) { _, method, args ->
        when (method.name) {
            "capabilities" -> buildJsonObject {
                put("server_id", "server")
                put("features", buildJsonObject { put("thread_handling", true) })
                put("device", buildJsonObject { put("scopes", JsonArray(if (readOnly) emptyList() else listOf(JsonPrimitive("edit")))) })
            }
            "now" -> if (fail) throw java.io.IOException("offline") else raw
            "edit" -> {
                sent += args[1] as JsonObject
                ContractFixtures["request_handling.complete"].responseBody!!.jsonObject
            }
            else -> error(method.name)
        }
    } as MobileApi
    private val controller by lazy { NowController("server", { api }, store) { now } }
    @After fun close() { store.close() }

    private fun show() {
        runBlocking { controller.refresh() }
        val model = object : PhoneReadModel {
            override val nowController = controller
            override suspend fun snapshot() = PhoneSnapshot(controller.snapshot!!.threads, emptyList(), emptyList())
        }
        compose.setContent { MaterialTheme { WeaverTheme { PhoneShell(model, StatusCounts(0, 0, 0), { now }, {}, {}) } } }
    }

    @Test fun tappingNowCardOpensInternalDetailAndConfirmationSendsHumanSentence() {
        show()
        val thread = controller.snapshot!!.threads.first()
        compose.onNodeWithText(thread.content).performScrollTo().performClick()
        compose.onNodeWithTag("now-thread-detail").assertExists()
        compose.onNodeWithText("출처: ${thread.source}").assertExists()
        compose.onNodeWithText("관련 이력").assertExists()
        compose.onNodeWithText("완료", useUnmergedTree = true).performScrollTo().performClick()
        compose.onNodeWithText("기록할 문장 확인").assertExists()
        assertTrue(sent.isEmpty())
        compose.onNodeWithText("확인하고 기록").performScrollTo().performClick()
        compose.waitUntil(10000) { sent.isNotEmpty() }
        assertEquals("이 요청을 완료했습니다: ${thread.content}", sent.single().getValue("content").jsonPrimitive.content)
        assertEquals(thread.revision, sent.single().getValue("expected_revision").jsonPrimitive.long)
    }

    @Test fun deadlineConfirmationSendsSelectedDateAndConfirmedSentence() {
        show()
        compose.onNodeWithText(controller.snapshot!!.threads.first().content).performScrollTo().performClick()
        compose.onNodeWithText("기한 변경").performScrollTo().performClick()
        compose.onNodeWithText("새 기한 (YYYY-MM-DD)").performScrollTo().performTextReplacement("2026-12-31")
        compose.onNodeWithText("확인하고 기록").performScrollTo().performClick()
        compose.waitUntil(10000) { sent.isNotEmpty() }
        assertEquals("2026-12-31", sent.single().getValue("deadline").jsonPrimitive.content)
        assertTrue(sent.single().getValue("content").jsonPrimitive.content.contains("2026-12-31"))
    }

    @Test fun nonRequestHasLocalHideButNoBusinessCompletionOrDeadline() {
        runBlocking { controller.refresh() }
        val thread = controller.snapshot!!.threads.first().copy(kind = ThreadKind.RECORD, open = false)
        compose.setContent { MaterialTheme { WeaverTheme { NowThreadSheet(thread, emptyList(), controller, {}, {}) } } }
        compose.onNodeWithText("완료").assertDoesNotExist()
        compose.onNodeWithText("기한 변경").assertDoesNotExist()
        compose.onNodeWithText("지금에서 숨기기").assertExists()
    }

    @Test fun closedRecentRequestShowsStatusAndNoCompletionMenu() {
        val thread = NowSnapshot.parse(raw).threads.first().copy(open = false, retracted = true)
        compose.setContent { MaterialTheme { WeaverTheme {
            NowThreadSheet(thread, emptyList(), null, {}, {})
        } } }
        compose.onNodeWithText("완료").assertDoesNotExist()
        compose.onNodeWithText("기한 변경").assertDoesNotExist()
        compose.onNodeWithText("기한 ${thread.deadline ?: "없음"} · 철회됨").assertExists()
    }

    @Test fun readOnlyTokenShowsDetailButNoHandlingMenu() {
        readOnly = true; show()
        compose.onNodeWithText("숨긴·미룬 항목").assertDoesNotExist()
        compose.onNodeWithText(controller.snapshot!!.threads.first().content).performScrollTo().performClick()
        compose.onNodeWithTag("now-thread-detail").assertExists()
        compose.onNodeWithText("완료").assertDoesNotExist()
        compose.onNodeWithText("기한 변경").assertDoesNotExist()
        compose.onNodeWithText("지금에서 숨기기").assertDoesNotExist()
    }

    @Test fun offlineCacheDisablesAcknowledgementAndBusinessWrites() {
        runBlocking { controller.refresh(); fail = true; controller.refresh() }
        val thread = controller.snapshot!!.threads.first()
        compose.setContent { MaterialTheme { WeaverTheme {
            androidx.compose.foundation.layout.Column {
                NowHandlingBar(controller, {}, {}, listOf(thread))
                NowThreadSheet(thread, emptyList(), controller, {}, {})
            }
        } } }
        compose.onNodeWithText("여기까지 확인").assertIsNotEnabled()
        compose.onNodeWithText("완료").assertIsNotEnabled()
        compose.onNodeWithText("기한 변경").assertIsNotEnabled()
        compose.onNodeWithText("지금에서 숨기기").assertIsEnabled()
        assertTrue(sent.isEmpty())
    }

    @Test fun hideAndRestoreAreLocalAndExplicitAckKeepsDueItems() {
        show(); val thread = controller.snapshot!!.threads.first()
        compose.onNodeWithText(thread.content).performScrollTo().performClick()
        compose.onNodeWithText("지금에서 숨기기").performScrollTo().performClick()
        compose.onNodeWithTag("now-thread-detail").assertDoesNotExist()
        compose.onNodeWithText("숨긴·미룬 항목").performClick()
        compose.onNodeWithText("다시 표시").performScrollTo().performClick()
        assertTrue(controller.suppressed().isEmpty())
        assertTrue(sent.isEmpty())
    }
}
