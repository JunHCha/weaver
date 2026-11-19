package io.github.junhcha.weaver.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.junit4.v2.createComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.longClick
import io.github.junhcha.weaver.phone.DictionaryEntry
import io.github.junhcha.weaver.phone.PhoneSnapshot
import io.github.junhcha.weaver.phone.PhoneTag
import io.github.junhcha.weaver.phone.Presentation
import io.github.junhcha.weaver.phone.SyntheticPhoneReadModel
import io.github.junhcha.weaver.phone.TagEditResult
import io.github.junhcha.weaver.phone.TagKind
import io.github.junhcha.weaver.phone.ThreadKind
import io.github.junhcha.weaver.transport.QueueCounts
import io.github.junhcha.weaver.transport.TransportFeature
import io.github.junhcha.weaver.transport.TransportRoute
import java.time.Clock
import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneId
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** A tag host that records what the sheet asked for. */
internal class RecordingTagHost(var entries: List<DictionaryEntry> = emptyList(), var failure: Throwable? = null) : TagEditHost {
    data class Call(val ids: List<String>, val add: List<String>, val remove: List<String>, val kinds: Map<String, TagKind>, val key: String)
    val calls = mutableListOf<Call>()
    val dictionaryCalls = mutableListOf<String>()
    override suspend fun dictionary() = entries
    override suspend fun editTags(threadIds: List<String>, add: List<String>, remove: List<String>, kinds: Map<String, TagKind>, key: String): TagEditResult {
        failure?.let { throw it }
        calls.add(Call(threadIds, add, remove, kinds, key)); return TagEditResult(threadIds.size, 0)
    }
    override suspend fun rename(entry: String, name: String, key: String) { dictionaryCalls.add("rename $entry -> $name") }
    override suspend fun setKind(entry: String, kind: TagKind, key: String) { dictionaryCalls.add("kind $entry $kind") }
    override suspend fun addAlias(entry: String, alias: String, key: String) { dictionaryCalls.add("alias+ $entry $alias") }
    override suspend fun removeAlias(entry: String, alias: String, key: String) { dictionaryCalls.add("alias- $entry $alias") }
    override suspend fun merge(entry: String, target: String, kind: TagKind?, key: String) { dictionaryCalls.add("merge $entry -> $target") }
}

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36], qualifiers = "w411dp-h1600dp-xhdpi")
class TransportScreensTest {
    @get:Rule val compose = createComposeRule()

    private val now: Instant = OffsetDateTime.parse("2026-11-18T09:00:00+09:00").toInstant()
    private val snapshot: PhoneSnapshot = runBlocking { SyntheticPhoneReadModel(Clock.fixed(now, ZoneId.of("UTC"))).snapshot() }
    private val tags = snapshot.tags

    // ---- connection / pairing ----

    private fun connection(ui: ConnectionUi, busy: Boolean = false, message: String? = null,
                           onPair: (String) -> Unit = {}) =
        compose.setContent {
            MaterialTheme { WeaverTheme { ConnectionContent(ui, busy, message, {}, onPair, {}, {}) } }
        }

    @Test fun unpairedShowsTheQrScanAndPasteFormAndSendsTheCode() {
        var sent: String? = null
        connection(ConnectionUi(), onPair = { sent = it })
        compose.onNodeWithTag("connection-status").assertIsDisplayed()
        compose.onNodeWithText("연결 안 됨").assertIsDisplayed()
        compose.onNodeWithTag("pair-scan").assertIsDisplayed()
        compose.onNodeWithTag("pair-submit").assertIsNotEnabled()
        compose.onNodeWithTag("pair-code").performTextInput("""{"v":1}""")
        compose.onNodeWithTag("pair-submit").assertIsEnabled().performClick()
        assertEquals("""{"v":1}""", sent)
    }

    @Test fun pairedShowsLastSyncQueueAndTheStatusWithoutTheForm() {
        val at = OffsetDateTime.parse("2026-11-18T08:30:00+09:00").toInstant().toEpochMilli()
        connection(ConnectionUi(true, "https://100.106.199.83:8787", false, at, null, QueueCounts(2, 1, 0, 0)))
        compose.onNodeWithText("연결됨").assertIsDisplayed()
        compose.onNodeWithTag("connection-last-sync").assertIsDisplayed()
        compose.onNodeWithText("마지막 동기화 11/18 08:30").assertIsDisplayed()
        compose.onNodeWithText("대기열 3건 · 수집 2 · 판정 1 · 실행 결과 0").assertIsDisplayed()
        compose.onAllNodesWithTag("pair-scan").assertCountEquals(0)
    }

    @Test fun statusExplainsWaitingRepairAndErrors() {
        assertEquals("연결 대기 · Mac이나 VPN에 닿지 않아요", ConnectionUi(true, lastError = "unreachable").statusText())
        assertTrue(ConnectionUi(true, needsRepair = true).statusText().startsWith("재페어링 필요"))
        assertEquals("오류 · invalid_schema", ConnectionUi(true, lastError = "invalid_schema").statusText())
        assertEquals("연결 안 됨", ConnectionUi().statusText())
    }

    @Test fun transportStateIsShownButNotSwitchableOnThePhone() {
        connection(ConnectionUi(true, "https://x:1", routes = mapOf(
            TransportFeature.CAPTURE to TransportRoute.HTTP, TransportFeature.VERDICT to TransportRoute.FILES,
            TransportFeature.ACTION_RESULT to TransportRoute.FILES), blockedFeatures = mapOf("action" to "not_executor")))
        compose.onNodeWithTag("route-capture").assertTextEquals("HTTP")
        compose.onNodeWithTag("route-review").assertTextEquals("Syncthing")
        compose.onNodeWithTag("route-action").assertTextEquals("막힘 · not_executor")
        compose.onNodeWithText("전송 경로 (서버 설정을 따라요)").assertIsDisplayed()
    }

    @Test fun anotherServersQueuedItemsAreWarnedAbout() {
        connection(ConnectionUi(true, "https://x:1", otherServerQueued = 3))
        compose.onNodeWithTag("other-server-warning").assertIsDisplayed()
    }

    @Test fun blockedItemsAreCountedWithARetry() {
        connection(ConnectionUi(true, "https://x:1", queue = QueueCounts(0, 0, 0, 2)))
        compose.onNodeWithText("조치가 필요한 항목 2건").assertIsDisplayed()
        compose.onNodeWithText("다시 시도").assertIsDisplayed()
    }

    // ---- tag edit sheet ----

    private val fact = snapshot.threads.first { it.id == "t-12" }
    private val history = snapshot.threads.first { it.id == "t-12-done" }

    private fun sheet(threads: List<io.github.junhcha.weaver.phone.PhoneThread>, entries: List<DictionaryEntry> = emptyList(),
                      error: String? = null, onSave: (List<String>, List<String>, Map<String, TagKind>) -> Unit = { _, _, _ -> }) =
        compose.setContent {
            MaterialTheme { WeaverTheme { TagEditContent(threads, tags, entries, false, error, {}, onSave) } }
        }

    @Test fun sheetAttachesAChipFoundByDictionarySearchAndSavesTheDiff() {
        var saved: Triple<List<String>, List<String>, Map<String, TagKind>>? = null
        sheet(listOf(fact)) { a, r, k -> saved = Triple(a, r, k) }
        compose.onNodeWithTag("tag-save").assertIsNotEnabled()
        compose.onNodeWithTag("tag-search").performTextInput("방화")
        compose.onNodeWithTag("tag-hit-방화벽 장비 교체").performClick()
        compose.onNodeWithTag("tag-chip-네트워크").performClick() // detach
        compose.onNodeWithTag("tag-save").assertIsEnabled().performClick()
        assertEquals(listOf("방화벽 장비 교체"), saved!!.first)
        assertEquals(listOf("네트워크"), saved!!.second)
    }

    @Test fun aSearchHitsAnAliasFromTheDictionary() {
        sheet(listOf(fact), entries = listOf(DictionaryEntry("방화벽 장비 교체", TagKind.PROJECT, listOf("fw-swap"), 1)))
        compose.onNodeWithTag("tag-search").performTextInput("fw-s")
        compose.onNodeWithTag("tag-hit-방화벽 장비 교체").assertIsDisplayed()
    }

    @Test fun aNewTagNeedsAKindAndIsCreatedThenAttached() {
        var saved: Triple<List<String>, List<String>, Map<String, TagKind>>? = null
        sheet(listOf(fact)) { a, r, k -> saved = Triple(a, r, k) }
        compose.onNodeWithTag("tag-search").performTextInput("신규 태그")
        compose.onNodeWithText("조직").performClick()
        compose.onNodeWithTag("tag-create").performClick()
        compose.onNodeWithTag("tag-save").performClick()
        assertEquals(listOf("신규 태그"), saved!!.first)
        assertEquals(mapOf("신규 태그" to TagKind.ORGANIZATION), saved!!.third)
    }

    @Test fun historyThreadsShowANoteInsteadOfTheEditor() {
        sheet(listOf(history))
        compose.onNodeWithTag("tag-history-note").assertIsDisplayed()
        compose.onAllNodesWithTag("tag-save").assertCountEquals(0)
    }

    @Test fun batchSheetShowsHowManyThreadsHaveEachTagAndSkipsHistoryRows() {
        val a = fact; val b = snapshot.threads.first { it.id == "t-10" }
        sheet(listOf(a, b, history))
        compose.onNodeWithText("3개 Thread 태그 편집").assertIsDisplayed()
        compose.onNodeWithText("이력 Thread 1개는 빼고 편집해요").assertIsDisplayed()
        compose.onNodeWithText("협력사 A 1/2").assertIsDisplayed()
    }

    @Test fun serverErrorIsShownInTheSheet() {
        sheet(listOf(fact), error = "서버에 연결할 수 없어요. 연결 후 다시 시도해 주세요")
        compose.onNodeWithTag("tag-error").assertIsDisplayed()
    }

    // ---- tag editing inside the person timeline ----

    @Test fun tappingAThreadOpensTheSheetAndLongPressSelectsSeveralForABatchEdit() {
        val host = RecordingTagHost()
        var edited = 0
        compose.setContent {
            MaterialTheme { WeaverTheme { TimelineScreen(snapshot, "이서준", {}, host) { edited++ } } }
        }
        compose.onNodeWithTag("thread-t-12").performClick()
        compose.onNodeWithTag("tag-sheet").assertIsDisplayed()
        compose.onNodeWithTag("tag-search").performTextInput("방화")
        compose.onNodeWithTag("tag-hit-방화벽 장비 교체").performClick()
        compose.onNodeWithTag("tag-save").performClick()
        compose.waitForIdle()
        assertEquals(listOf("t-12"), host.calls.single().ids)
        assertEquals(1, edited)
        // Long press starts a multi-selection; a tap then adds a second Thread.
        compose.onNodeWithTag("thread-t-14a").performTouchInput { longClick() }
        compose.onNodeWithTag("thread-t-10").performClick()
        compose.onNodeWithText("2개 선택").assertIsDisplayed()
        compose.onNodeWithTag("batch-edit").performClick()
        compose.onNodeWithText("2개 Thread 태그 편집").assertIsDisplayed()
    }

    @Test fun withoutAPairingTheTimelineStaysReadOnly() {
        compose.setContent { MaterialTheme { WeaverTheme { TimelineScreen(snapshot, "이서준", {}) } } }
        compose.onAllNodesWithTag("dictionary-open").assertCountEquals(0)
        compose.onAllNodesWithTag("tag-sheet").assertCountEquals(0)
    }

    @Test fun dictionarySheetRenamesAddsAnAliasAndMerges() {
        val host = RecordingTagHost(listOf(DictionaryEntry("네트워크", TagKind.TOPIC, listOf("net"), 4)))
        compose.setContent {
            MaterialTheme { WeaverTheme {
                var rename: String? = null
                DictionaryContent("네트워크", host.entries.single(), tags, false, null, {},
                    onRename = { rename = it; host.dictionaryCalls.add("rename $it") },
                    onKind = { host.dictionaryCalls.add("kind $it") },
                    onAddAlias = { host.dictionaryCalls.add("alias+ $it") },
                    onRemoveAlias = { host.dictionaryCalls.add("alias- $it") },
                    onMerge = { t, k -> host.dictionaryCalls.add("merge $t $k") })
            } }
        }
        compose.onNodeWithText("사전: 네트워크").assertIsDisplayed()
        compose.onNodeWithText("net ✕").performClick()
        compose.onNodeWithText("조직").performClick()
        assertEquals(listOf("alias- net", "kind ORGANIZATION"), host.dictionaryCalls)
    }
    @Test fun readErrorsAndSkippedCountsAreVisible() {
        val code = "unknown_thread_kind:1,invalid_thread:1,invalid_tag:2,invalid_wiki_page:1"
        connection(ConnectionUi(true, lastError = "read:$code"))
        compose.onNodeWithText("읽기 오류 · $code · 5개 건너뜀").assertIsDisplayed()
        assertEquals("연결 대기 · Mac이나 VPN에 닿지 않아요", ConnectionUi(true, lastError = "read:unreachable").statusText())
        assertEquals("연결 대기 · Mac이나 VPN에 닿지 않아요", ConnectionUi(true, lastError = "read:invalid_tag:2,unreachable").statusText())
        assertEquals("읽기 오류 · 응답 형식이 올바르지 않아요", ConnectionUi(true, lastError = "read:invalid_read_response").statusText())
    }

}
