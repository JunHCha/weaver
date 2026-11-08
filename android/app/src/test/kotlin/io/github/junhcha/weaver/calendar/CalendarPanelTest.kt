package io.github.junhcha.weaver.calendar

import android.Manifest
import android.app.Application
import android.content.pm.ProviderInfo
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
    private fun setup(blocked: Boolean = false): Pair<Application, CalendarRuntimeTest.RecordingProvider> {
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
    @Test fun blockedScheduleShowsRegistrationBlockAndNeverWrites() {
        val (_, provider) = setup(blocked = true)
        compose.onNodeWithText("요청 미해소 · 등록 차단", substring = true).assertExists()
        compose.onNodeWithText("승인 일정 등록 / 등록만 재시도").assertIsNotEnabled()
        assertTrue(provider.inserts.isEmpty())
    }
}
