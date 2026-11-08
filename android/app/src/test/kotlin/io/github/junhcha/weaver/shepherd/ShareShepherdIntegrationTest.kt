package io.github.junhcha.weaver.shepherd

import android.Manifest
import android.app.Application
import android.content.ContentProvider
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.database.MatrixCursor
import android.net.Uri
import android.os.Looper
import android.os.ParcelFileDescriptor
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.v2.createEmptyComposeRule
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.work.Configuration
import androidx.work.WorkManager
import androidx.work.testing.SynchronousExecutor
import androidx.work.testing.WorkManagerTestInitHelper
import io.github.junhcha.weaver.courier.CourierDatabase
import io.github.junhcha.weaver.schema.Channel
import java.io.File
import java.time.ZoneId
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowContentResolver

/** Real Runtime/SQLite/adapter/submission with only MediaStore and scheduling replaced. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class ShareShepherdIntegrationTest {
    @get:Rule val compose = createEmptyComposeRule()
    private lateinit var app: Application
    private val zone = ZoneId.of("Asia/Seoul")
    private val now = System.currentTimeMillis()
    private var rows = emptyList<RecordingMetadata>()
    @Volatile private var queries = 0
    @Volatile private var queriedOnMain = false

    private fun row(id: Int, name: String) = RecordingMetadata(
        "content://media/external/audio/media/$id", name, 100, now / 1000, now,
    )

    @Before fun setup() {
        app = ApplicationProvider.getApplicationContext()
        app.deleteDatabase("shepherd.sqlite3")
        app.deleteDatabase(File(app.filesDir, "courier.sqlite3").absolutePath)
        ShepherdRuntime.activatedCache = null
        listOf("courier", "shepherd-call-links", "shepherd-call-drafts", "shepherd-call-assignments").forEach {
            app.getSharedPreferences(it, Context.MODE_PRIVATE).edit().clear().commit()
        }
        File(app.filesDir, "courier-staging").deleteRecursively()
        File(app.filesDir, "shepherd-shares").deleteRecursively()
        shadowOf(app).grantPermissions(Manifest.permission.READ_MEDIA_AUDIO)
        WorkManagerTestInitHelper.initializeTestWorkManager(app, Configuration.Builder()
            .setExecutor(SynchronousExecutor()).build())
        ShepherdDatabase(app).use { it.activate(now - 10_000) }
        ShadowContentResolver.registerProviderInternal("media", object : ContentProvider() {
            override fun onCreate() = true
            override fun query(uri: Uri, projection: Array<out String>?, selection: String?, args: Array<out String>?, sort: String?) =
                MatrixCursor(projection!!).apply {
                    queries++
                    if (Looper.myLooper() == Looper.getMainLooper()) queriedOnMain = true
                    rows.forEach { row -> addRow(arrayOf<Any>(row.uri.substringAfterLast('/').toLong(),
                        row.name, row.size, row.modified, row.addedAt / 1000, "Recordings/Call/")) }
                }
            override fun openFile(uri: Uri, mode: String): ParcelFileDescriptor = error("Audio must never be opened")
            override fun getType(uri: Uri): String? = null
            override fun insert(uri: Uri, values: ContentValues?): Uri? = error("No media writes")
            override fun delete(uri: Uri, selection: String?, args: Array<out String>?): Int = error("No media writes")
            override fun update(uri: Uri, values: ContentValues?, selection: String?, args: Array<out String>?): Int = error("No media writes")
        })
    }

    @After fun cleanup() {
        WorkManager.getInstance(app).cancelAllWork().result.get()
        WorkManagerTestInitHelper.closeWorkDatabase()
    }

    private fun launch() = ActivityScenario.launch<ShareActivity>(Intent(app, ShareActivity::class.java)
        .setAction(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, "실제 통화 스크립트 테스트"))

    private fun ready() {
        compose.waitUntil(10_000) {
            compose.onAllNodes(hasText("bytes 준비됨", substring = true)).fetchSemanticsNodes().isNotEmpty()
        }
    }

    @Test fun shareScansOnIoBeforeSelectionAndEarlyProcessingSurvivesGrowthAndRestart() {
        val early = row(1, "Person_261108_123456.m4a")
        rows = listOf(early)
        assertTrue(ShepherdRuntime.pendingCalls(app).isEmpty())
        launch().use { scenario ->
            ready()
            assertTrue(queries > 0)
            assertFalse(queriedOnMain)
            assertTrue(ShepherdRuntime.calls(app).isEmpty()) // still OBSERVING
            compose.onNodeWithTag("channel-CALL").assertIsEnabled().performClick()
            compose.onNode(hasText("● ", substring = true) and hasText(early.name, substring = true)).assertExists()
            scenario.recreate()
            ready()
            compose.onNodeWithTag("channel-CALL").assertIsSelected()
            compose.onNodeWithText("Courier로 보내기").performScrollTo().performClick()
            compose.waitUntil(10_000) {
                compose.onAllNodes(hasText("Courier 등록 완료:", substring = true)).fetchSemanticsNodes().isNotEmpty()
            }
        }
        val capture = CourierDatabase(app).use { it.captures().single() }
        val call = ShepherdRuntime.pendingCalls(app).single()
        assertEquals(Channel.CALL, capture.manifest.channel)
        assertEquals(call.occurredAt, capture.manifest.occurredAt)
        assertEquals("Person", capture.manifest.hints.counterpart)
        assertTrue(PendingCalls.reconcile(app))
        assertEquals(call.id, ShepherdRuntime.pendingCalls(app).single().id) // queued only
        CourierDatabase(app).use { it.state(capture.manifest.captureId, "SENT", now) }
        assertTrue(PendingCalls.reconcile(app)) // reserves completion of OBSERVING
        assertTrue(app.getSharedPreferences("shepherd-call-links", Context.MODE_PRIVATE).all.isEmpty())
        assertFalse(ShepherdRuntime.markProcessed(app, call.id))
        assertTrue(PendingCalls.repository(app).pending().isEmpty())
        val grown = early.copy(size = 200, modified = early.modified + 1)
        ShepherdDatabase(app).use { it.observe(listOf(grown), now + 1_000, zone) }
        // Reopen both stores, then observe the full stabilization interval.
        ShepherdDatabase(app).use { db ->
            db.observe(listOf(grown), now + 31_000, zone)
            assertEquals(CallStatus.PROCESSED, db.calls().single().status)
        }
        assertTrue(PendingCalls.reconcile(app))
        assertTrue(PendingCalls.repository(app).pending().isEmpty())
        val replay = SharedText.receive(app, Intent(Intent.ACTION_SEND).setType("text/plain")
            .putExtra(Intent.EXTRA_TEXT, "실제 통화 스크립트 테스트"))
        assertTrue(ShareSubmission.submit(app, replay, Channel.CALL, call.id).duplicate)
        CourierDatabase(app).use { assertEquals(1, it.captures().size) }
    }

    @Test fun realListExcludesTerminalStatesAndSortsKnownTimesBeforeUnknown() {
        val stable = listOf(row(1, "Older_261108_123456.m4a"), row(2, "Recent_261108_133456.m4a"),
            row(3, "unknown.m4a"), row(4, "Processed_261108_143456.m4a"),
            row(5, "Dismissed_261108_153456.m4a"), row(6, "Removed_261108_163456.m4a"))
        ShepherdDatabase(app).use { db ->
            db.observe(stable, now - 31_000, zone)
            db.observe(stable, now - 1_000, zone)
            assertTrue(db.markProcessed(stable[3].uri))
            assertTrue(db.dismiss(stable[4].uri))
        }
        rows = stable.dropLast(1)
        launch().use {
            ready()
            compose.onNodeWithTag("channel-CALL").performClick()
            compose.onNode(hasText("● ", substring = true) and hasText("Recent_", substring = true)).assertExists()
            compose.onNode(hasText("시각 미상 · unknown.m4a", substring = true)).assertExists()
            listOf("Processed_", "Dismissed_", "Removed_").forEach { name ->
                compose.onNode(hasText(name, substring = true)).assertDoesNotExist()
            }
        }
        assertFalse(queriedOnMain)
        assertEquals(listOf(stable[1].uri, stable[0].uri, stable[2].uri),
            PendingCalls.repository(app).pending().map { it.id })
        assertEquals(CallStatus.REMOVED, ShepherdRuntime.calls(app).single { it.id == stable[5].uri }.status)
    }
}
