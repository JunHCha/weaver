package io.github.junhcha.weaver.calendar

import android.Manifest
import android.app.Application
import android.content.ContentValues
import android.content.pm.ProviderInfo
import android.provider.CalendarContract.Events
import android.provider.CalendarContract
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.test.core.app.ApplicationProvider
import io.github.junhcha.weaver.MainActivity
import io.github.junhcha.weaver.review.*
import io.github.junhcha.weaver.schema.*
import io.github.junhcha.weaver.storage.AppStorage
import io.github.junhcha.weaver.ui.CalendarPanel
import kotlinx.serialization.json.Json
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowContentResolver
import java.io.File

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class CalendarPanelTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()
    private fun setup(blocked: Boolean = false,
                      configure: (Application, CalendarRuntimeTest.RecordingProvider, ReviewRecord) -> Unit = { _, _, _ -> }
    ): Pair<Application, CalendarRuntimeTest.RecordingProvider> {
        val app = ApplicationProvider.getApplicationContext<Application>()
        File(AppStorage.exchangeDir(app), "calendar").deleteRecursively()
        File(AppStorage.exchangeDir(app), "reviews").deleteRecursively()
        val provider = CalendarRuntimeTest.RecordingProvider()
        provider.attachInfo(app, ProviderInfo().apply { authority = CalendarContract.AUTHORITY })
        ShadowContentResolver.registerProviderInternal(CalendarContract.AUTHORITY, provider)
        val p = WeaverCodec.json.decodeFromString<Proposal>(File(System.getProperty("weaver.schemaDir"), "examples/Proposal.json").readText())
        val snapshot = p.copy(entities = p.entities.map { it.copy(ask = null) },
            schedules = p.schedules.map { it.copy(blockedByRequest = if (blocked) "d1" else null) })
        val verdict = Verdict(1, p.proposalId, p.rev, Verdict.Kind.APPROVE, emptyList(), null, null, "2026-11-08T00:00:00Z")
        val record = ReviewRecord(verdict, "synthetic", ReviewStatus.INFERRED, published = true, proposal = snapshot)
        File(AppStorage.exchangeDir(app), "reviews/${p.proposalId}.r${p.rev}.json").apply {
            parentFile!!.mkdirs(); writeText(Json.encodeToString(record))
        }
        configure(app, provider, record)
        compose.runOnUiThread {
            compose.activity.setContent {
                MaterialTheme { Column(Modifier.verticalScroll(rememberScrollState())) { CalendarPanel(record) } }
            }
        }
        return app to provider
    }
    @Test fun permissionRetryAndCalendarSelectionRequireExplicitRegistrationTap() {
        val (app, provider) = setup()
        compose.onNodeWithText("종료 시각 없음, 1시간으로 등록", substring = true).assertExists()
        compose.onNodeWithText("승인 일정 등록 / 등록만 재시도").assertIsNotEnabled()
        assertTrue(provider.inserts.isEmpty())
        // Denied on entry; granting permissions later never loses the published review.
        shadowOf(app).grantPermissions(Manifest.permission.READ_CALENDAR, Manifest.permission.WRITE_CALENDAR)
        compose.onNodeWithText("캘린더 선택 / 권한 재요청").performScrollTo().performClick()
        val label = "Synthetic calendar · 계정 synthetic@example.invalid · 종류 LOCAL"
        compose.waitUntil(10_000) { compose.onAllNodesWithText(label).fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText(label).performScrollTo().performClick()
        compose.waitUntil(10_000) { compose.onAllNodesWithText("선택됨:", substring = true).fetchSemanticsNodes().isNotEmpty() }
        assertTrue(provider.inserts.isEmpty())
        compose.onNodeWithText("승인 일정 등록 / 등록만 재시도").performScrollTo().performClick()
        compose.waitUntil(10_000) { compose.onAllNodesWithText("${ReviewRuntime.records(app).single().proposal!!.schedules.single().title}: 등록됨").fetchSemanticsNodes().isNotEmpty() }
        assertEquals(1, provider.inserts.size)
        assertTrue(ReviewRuntime.records(app).single().published)
    }
    @Test fun deletedTargetNeedsSelectionAndSeparateNewRegistrationTap() {
        val (app, provider) = setup { app, _, record ->
            shadowOf(app).grantPermissions(Manifest.permission.READ_CALENDAR, Manifest.permission.WRITE_CALENDAR)
            val old = CalendarTarget(72, "Removed", "synthetic@example.invalid", "LOCAL")
            val p = record.proposal!!
            val journal = CalendarJournal(File(AppStorage.exchangeDir(app), "calendar/registrations.json"))
            journal.select(old)
            journal.put(CalendarMapping(p.proposalId, p.rev, p.schedules.single().ref, old,
                event = CalendarEvent.from(p.schedules.single(), "Asia/Seoul")))
        }
        val ref = ReviewRuntime.records(app).single().proposal!!.schedules.single().ref
        val action = "$ref: 선택한 새 대상에 새로 등록"
        compose.waitUntil(10_000) { compose.onAllNodesWithText(action).fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("캘린더 선택 / 권한 재요청").performScrollTo().performClick()
        val label = "Synthetic calendar · 계정 synthetic@example.invalid · 종류 LOCAL"
        compose.waitUntil(10_000) { compose.onAllNodesWithText(label).fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText(label).performScrollTo().performClick()
        compose.waitUntil(10_000) { compose.onAllNodesWithText("선택됨: Synthetic calendar", substring = true).fetchSemanticsNodes().isNotEmpty() }
        assertTrue(provider.inserts.isEmpty())
        compose.onNodeWithText(action).performScrollTo().performClick()
        compose.waitUntil(10_000) { compose.onAllNodesWithText("${ReviewRuntime.records(app).single().proposal!!.schedules.single().title}: 등록됨").fetchSemanticsNodes().isNotEmpty() }
        assertEquals(1, provider.inserts.size)
        assertEquals(71L, provider.inserts.single().getAsLong(Events.CALENDAR_ID))
    }

    @Test fun copiedMarkerNeedsExplicitManualConfirmationBeforeNewWrite() {
        val (app, provider) = setup { app, provider, record ->
            shadowOf(app).grantPermissions(Manifest.permission.READ_CALENDAR, Manifest.permission.WRITE_CALENDAR)
            val p = record.proposal!!
            val target = CalendarTarget(71, "Synthetic calendar", "synthetic@example.invalid", "LOCAL")
            val event = CalendarEvent.from(p.schedules.single(), "Asia/Seoul")
            val pending = CalendarMapping(p.proposalId, p.rev, p.schedules.single().ref, target, event = event)
            val journal = CalendarJournal(File(AppStorage.exchangeDir(app), "calendar/registrations.json"))
            journal.select(target); journal.put(pending)
            provider.inserts += ContentValues().apply {
                put(Events.DESCRIPTION, pending.marker); put(Events.CALENDAR_ID, 999L)
                put(Events.TITLE, event.title); put(Events.DTSTART, event.start + 60_000)
                put(Events.DTEND, event.end); put(Events.EVENT_TIMEZONE, event.timezone)
            }
        }
        val ref = ReviewRuntime.records(app).single().proposal!!.schedules.single().ref
        val action = "$ref: 확인했음 · 새로 등록"
        compose.waitUntil(10_000) { compose.onAllNodesWithText(action).fetchSemanticsNodes().isNotEmpty() }
        assertEquals(1, provider.inserts.size) // Fixture only: recovery is read-only.
        compose.onNodeWithText("승인 일정 등록 / 등록만 재시도").performScrollTo().performClick()
        val title = ReviewRuntime.records(app).single().proposal!!.schedules.single().title
        compose.waitUntil(10_000) { compose.onAllNodesWithText("$title: 수동 확인 필요 · 확인 후 새로 등록").fetchSemanticsNodes().isNotEmpty() }
        compose.waitForIdle()
        assertEquals(1, provider.inserts.size)
        compose.onNodeWithText(action).performScrollTo().performClick()
        compose.waitUntil(10_000) { compose.onAllNodesWithText("${ReviewRuntime.records(app).single().proposal!!.schedules.single().title}: 등록됨").fetchSemanticsNodes().isNotEmpty() }
        assertEquals(2, provider.inserts.size)
    }

    @Test fun legacyApprovalExplainsMissingSnapshotWithoutWriting() {
        val (_, provider) = setup()
        val app = ApplicationProvider.getApplicationContext<Application>()
        val legacy = ReviewRuntime.records(app).single().copy(proposal = null)
        compose.runOnUiThread { compose.activity.setContent { MaterialTheme { CalendarPanel(legacy) } } }
        compose.onNodeWithText("원안 스냅샷이 없는 이전 승인 이력은 캘린더에 등록할 수 없습니다").assertExists()
        compose.onNodeWithText("승인 일정 등록 / 등록만 재시도").assertDoesNotExist()
        assertTrue(provider.inserts.isEmpty())
    }

    @Test fun blockedScheduleShowsRegistrationBlockAndNeverWrites() {
        val (_, provider) = setup(blocked = true)
        compose.onNodeWithText("요청 미해소 · 등록 차단", substring = true).assertExists()
        compose.onNodeWithText("승인 일정 등록 / 등록만 재시도").assertIsNotEnabled()
        assertTrue(provider.inserts.isEmpty())
    }
}
