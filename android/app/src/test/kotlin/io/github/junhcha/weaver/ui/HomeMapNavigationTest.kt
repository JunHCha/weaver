package io.github.junhcha.weaver.ui

import android.content.Context
import android.content.Intent
import android.os.Looper
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.lifecycle.ViewModelProvider
import androidx.test.core.app.ApplicationProvider
import io.github.junhcha.weaver.MainActivity
import io.github.junhcha.weaver.calendar.ActionState
import io.github.junhcha.weaver.calendar.CalendarJournal
import io.github.junhcha.weaver.calendar.CalendarRuntime
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.pipeline.PipelineStore
import io.github.junhcha.weaver.pipeline.PipelineViewModel
import io.github.junhcha.weaver.pipeline.MapNode
import io.github.junhcha.weaver.schema.*
import io.github.junhcha.weaver.transport.*
import io.github.junhcha.weaver.storage.AppStorage
import java.io.File
import java.lang.reflect.Proxy
import java.text.Normalizer
import java.time.OffsetDateTime
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

/** Paired home navigation with synthetic observations, without network or a user vault. */
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [36], qualifiers = "w411dp-h891dp-port-xhdpi")
class HomeMapNavigationTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()
    private val originalApiFactory = TransportRuntime.apiFactory
    private val originalSecretBox = TransportRuntime.secretBox
    private val context get() = ApplicationProvider.getApplicationContext<Context>()
    private val at = "2026-11-19T01:00:00Z"
    private val apiCreatedOnMain = AtomicBoolean(false)
    private val apiCreations = AtomicInteger()
    @Volatile private var failApiConstruction = false
    private var initializedWorkManager = false
    private val items = listOf(
        observation(1, PipelineLocation.USER, PipelineStatus.REVIEW).copy(packageId = "synthetic-review", packageRevision = 1),
        observation(2, PipelineLocation.FABRIC, PipelineStatus.DONE).copy(
            summary = "검색 시안을 다음 주 검토하기로 했다.", counterpart = "민수",
            relatedTags = listOf(PipelineTag("검색", "area"), PipelineTag("민수", "person"), PipelineTag("미분류"), PipelineTag("없는사람", "person")),
        ),
        observation(3, PipelineLocation.WIKI, PipelineStatus.DONE).copy(title = Normalizer.normalize("합성 장비 점검", Normalizer.Form.NFD), pages = listOf(
            PipelinePage("pages/" + Normalizer.normalize("합성 장비 점검", Normalizer.Form.NFD) + ".md", OffsetDateTime.now().toString(), "a".repeat(64)),
        )),
        observation(4, PipelineLocation.LOOM, PipelineStatus.REJECTED).copy(threads = emptyList(), captureHosts = emptyList()),
        observation(5, PipelineLocation.LOOM, PipelineStatus.APPLYING).copy(threads = listOf(PipelineThread("action-5", "일정", "합성 캘린더 등록 실행 대기"))),
        observation(6, PipelineLocation.LOOM, PipelineStatus.APPLY_STOPPED),
    )

    private fun observation(index: Int, location: PipelineLocation, status: PipelineStatus) = PipelineItem(
        2, 1, "cap_01ARZ3NDEKTSV4RRFFQ69G5FA$index", "src_01ARZ3NDEKTSV4RRFFQ69G5FA$index",
        null, null, "call", "합성 장비 점검 $index · " + "담당자와 확인할 긴 제목 ".repeat(12), location, status,
        mapOf(Stage.REVIEW to at, Stage.APPLY to at), listOf(PipelineLocation.SPINNER, PipelineLocation.LOOM, location),
        listOf(PipelineThread("thread-$index", "요청", "합성 점검 결과를 공유해 주세요")), emptyList(), null, 5, at,
        captureHosts = listOf(CaptureHost.PHONE, CaptureHost.MAC),
    )

    @Before fun seedPairedHome() {
        TransportRuntime.secretBox = { ReverseBox() }
        TransportRuntime.apiFactory = {
            apiCreatedOnMain.compareAndSet(false, Looper.myLooper() == Looper.getMainLooper())
            apiCreations.incrementAndGet()
            // Exercise the real TLS constructor against synthetic corrupt persisted CA bytes.
            if (failApiConstruction) originalApiFactory(it)
            else Proxy.newProxyInstance(MobileApi::class.java.classLoader, arrayOf(MobileApi::class.java)) { _, method, _ ->
                    when (method.name) {
                        "threads", "wikiPages" -> Page(emptyList(), null)
                        "tags" -> listOf("{\"name\":\"민수\",\"kind\":\"person\"}", "{\"name\":\"검색\",\"kind\":\"area\"}").map { kotlinx.serialization.json.Json.parseToJsonElement(it) }
                        else -> error("Unexpected API call in synthetic navigation test: ${method.name}")
                    }
                } as MobileApi
        }
        val pki = TestPki.default
        TransportRuntime.settings(context).pair(Pairing("https://synthetic.invalid", "synthetic-server", "synthetic-phone", "synthetic-token", pki.caSha256, pki.caDer))
        PipelineStore(File(context.filesDir, "exchange/pipeline")).merge("", items)
        CalendarJournal(File(AppStorage.exchangeDir(context), "calendar/registrations.json")).put(
            ActionState("act_" + "1".repeat(32), ActionKind.REGISTER, "합성 캘린더 등록 실행 대기"),
        )
    }

    @After fun resetTransport() {
        if (initializedWorkManager) {
            androidx.work.WorkManager.getInstance(context).cancelAllWork().result.get()
            androidx.work.testing.WorkManagerTestInitHelper.closeWorkDatabase()
        }
        context.getSharedPreferences("transport", Context.MODE_PRIVATE).edit().clear().commit()
        TransportRuntime.apiFactory = originalApiFactory
        TransportRuntime.secretBox = originalSecretBox
    }

    private fun home() {
        val model = ViewModelProvider(compose.activity)[PipelineViewModel::class.java]
        model.refresh()
        compose.waitUntil(10_000) { model.state.value.loaded && model.state.value.items.size == items.size }
        assertEquals(items.size, model.state.value.items.size)
        assertEquals("합성 캘린더 등록 실행 대기", CalendarRuntime.actions(context).single().title)
    }

    @Test fun pairedStatusStripOpensPopulatedMapAndReturnsToNow() {
        home()
        compose.onNodeWithTag("status-strip").performClick()
        compose.onNodeWithText("내 데이터 흐름").assertIsDisplayed()
        compose.onNodeWithContentDescription("You 노드").performClick()
        compose.onNodeWithText("엮은 내용을 확인하고 승인해 주세요").assertIsDisplayed()
        compose.onNodeWithText("닫기").performClick()
        compose.onNodeWithText("‹ 지금").performClick()
        compose.onNodeWithTag("now-screen").assertIsDisplayed()
    }

    @Test fun inboxNotificationOpensPopulatedMapAtYouNodeOnce() {
        home()
        val originalIntent = compose.activity.intent
        val tap = Intent(context, MainActivity::class.java).setAction(InboxRuntime.OPEN_INBOX)
        compose.runOnUiThread {
            MainActivity::class.java.getDeclaredMethod("onNewIntent", Intent::class.java).apply {
                isAccessible = true
            }.invoke(compose.activity, tap)
        }
        compose.onNodeWithText("내 데이터 흐름").assertIsDisplayed()
        compose.onNodeWithText("엮은 내용을 확인하고 승인해 주세요").assertIsDisplayed()
        compose.runOnIdle { assertNull(compose.activity.intent.action) }
        compose.onNodeWithText("닫기").performClick()
        compose.onNodeWithText("‹ 지금").performClick()
        compose.onNodeWithTag("now-screen").assertIsDisplayed()
        compose.runOnUiThread { compose.activity.intent = originalIntent }
    }

    @Test
    @Config(qualifiers = "w891dp-h411dp-land-xhdpi", fontScale = 2f)
    fun compactLandscapeStatusStripOpensMapWithLargeText() {
        home()
        compose.onNodeWithTag("status-strip").performClick()
        compose.onNodeWithText("내 데이터 흐름").assertIsDisplayed()
        compose.onNodeWithContentDescription("You 노드").performClick()
        compose.onNodeWithText("엮은 내용을 확인하고 승인해 주세요").assertExists()
    }

    @Test fun pairedApiConstructionStaysOffMainThreadDuringNavigationAndRefresh() {
        home()
        compose.waitUntil(10_000) { apiCreations.get() > 0 }
        compose.onNodeWithTag("status-strip").performClick()
        ViewModelProvider(compose.activity)[PipelineViewModel::class.java].refresh()
        compose.waitForIdle()
        assertFalse("Pairing, Keystore and TLS client construction must stay off the UI thread", apiCreatedOnMain.get())
    }

    @Test fun pairedApiConstructionFailureKeepsStatusStripAndMapUsable() {
        home()
        compose.onNodeWithTag("status-strip").performClick()
        compose.waitUntil(10_000) { apiCreations.get() > 0 }
        val before = apiCreations.get()
        context.getSharedPreferences("transport", Context.MODE_PRIVATE).edit().putString("ca_der", "AQ==").commit()
        failApiConstruction = true
        val model = ViewModelProvider(compose.activity)[PipelineViewModel::class.java]
        model.endVisit()
        model.refresh()
        compose.waitUntil(10_000) { apiCreations.get() > before }
        compose.onNodeWithText("내 데이터 흐름").assertIsDisplayed()
        compose.onNodeWithContentDescription("You 노드").performClick()
        compose.onNodeWithText("엮은 내용을 확인하고 승인해 주세요").assertIsDisplayed()
    }

    @Test fun pairedMapOpensEveryNodeWithMixedAndMissingOptionalFields() {
        home()
        compose.onNodeWithTag("status-strip").performClick()
        for (node in MapNode.entries.filter { it != MapNode.COURIER }) {
            compose.onNodeWithContentDescription("${node.label} 노드").performClick()
            compose.onNodeWithText("닫기").assertIsDisplayed()
            if (node == MapNode.WIKI) {
                compose.onAllNodesWithText("합성 장비 점검").assertCountEquals(2)
                compose.onNodeWithText("Obsidian vault 이름").performScrollTo().assertIsEnabled()
            }
            compose.onNodeWithText("닫기").performScrollTo().performClick()
        }
    }
    @Test fun fabricCardOpensExistingPersonAndAreaTimelinesAndRestoresMapAfterRecreation() {
        home()
        compose.onNodeWithTag("status-strip").performClick()
        compose.onNodeWithContentDescription("Fabric 노드").performClick()
        compose.onNodeWithText("검색 시안을 다음 주 검토하기로 했다.").assertIsDisplayed()
        compose.onNodeWithText("출처 통화 녹음 · 폰 · Mac").assertExists()
        compose.onNodeWithText("#미분류").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithText("#없는사람").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithText("#검색").performScrollTo().assertIsEnabled().performClick()
        compose.onNodeWithTag("timeline-screen").assertIsDisplayed()
        compose.onNodeWithText("검색", useUnmergedTree = true).assertExists()
        compose.onNodeWithContentDescription("뒤로").performClick()
        compose.onNodeWithText("내 데이터 흐름").assertIsDisplayed()
        compose.onNodeWithText("상대 민수").performScrollTo().assertIsEnabled().performClick()
        compose.onNodeWithTag("timeline-screen").assertIsDisplayed()
        // Recreated MainActivity schedules background work: keep every worker synthetic too.
        val factory = object : androidx.work.WorkerFactory() {
            override fun createWorker(appContext: Context, workerClassName: String, workerParameters: androidx.work.WorkerParameters): androidx.work.ListenableWorker =
                object : androidx.work.Worker(appContext, workerParameters) {
                    override fun doWork() = Result.success()
                }
        }
        androidx.work.testing.WorkManagerTestInitHelper.initializeTestWorkManager(context,
            androidx.work.Configuration.Builder().setExecutor(androidx.work.testing.SynchronousExecutor()).setWorkerFactory(factory).build())
        initializedWorkManager = true
        compose.activityRule.scenario.recreate()
        compose.waitUntil(10_000) { compose.onNodeWithTag("timeline-screen").isDisplayed() }
        compose.onNodeWithTag("timeline-screen").assertIsDisplayed()
        compose.runOnUiThread { compose.activity.onBackPressedDispatcher.onBackPressed() }
        compose.onNodeWithText("내 데이터 흐름").assertIsDisplayed()
        compose.onNodeWithText("검색 시안을 다음 주 검토하기로 했다.").performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("리마인더로 전환").assertDoesNotExist()
        compose.onNodeWithText("메모 추가").assertDoesNotExist()
    }

}
