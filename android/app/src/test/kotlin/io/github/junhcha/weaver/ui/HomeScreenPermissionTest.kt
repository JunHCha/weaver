package io.github.junhcha.weaver.ui

import android.Manifest
import android.app.Application
import android.app.NotificationManager
import android.content.Intent
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import io.github.junhcha.weaver.MainActivity
import io.github.junhcha.weaver.shepherd.RecordingMetadata
import io.github.junhcha.weaver.shepherd.ShepherdDatabase
import io.github.junhcha.weaver.shepherd.ShepherdRuntime
import java.time.ZoneId
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

/** The home screen must open with every feature permission denied, and with a mix. */
@RunWith(AndroidJUnit4::class)
class HomeScreenPermissionTest {
    @get:Rule
    val compose = createAndroidComposeRule<MainActivity>()

    @Test
    @Config(sdk = [36])
    fun opensWithAllPermissionsDenied() {
        val context = ApplicationProvider.getApplicationContext<Application>()
        Feature.entries.forEach { assertTrue("$it must start denied", !it.isGranted(context)) }

        compose.onNodeWithText("내 데이터 흐름").assertExists()
        compose.onNodeWithText("수집·설정").performClick()
        compose.onNodeWithText("Weaver").assertExists()
        compose.onAllNodesWithText("권한 없음").assertCountEquals(Feature.entries.size)
        compose.onAllNodesWithText("권한 요청").assertCountEquals(Feature.entries.size)
    }

    @Test
    @Config(sdk = [26])
    fun opensOnMinSdkWithPartialGrant() {
        // Notifications need no runtime permission before Android 13.
        val app = ApplicationProvider.getApplicationContext<Application>()
        shadowOf(app).grantPermissions(Manifest.permission.READ_CALENDAR, Manifest.permission.WRITE_CALENDAR)
        compose.activityRule.scenario.recreate()

        compose.onNodeWithText("내 데이터 흐름").assertExists()
        compose.onNodeWithText("수집·설정").performClick()
        compose.onNodeWithText("Weaver").assertExists()
        compose.onAllNodesWithText("허용됨").assertCountEquals(2)
        compose.onAllNodesWithText("권한 없음").assertCountEquals(1)
    }

    @Test
    @Config(sdk = [36])
    fun notificationTapOpensPendingListAndUserCanDismissCall() {
        val app = ApplicationProvider.getApplicationContext<Application>()
        app.deleteDatabase("shepherd.sqlite3")
        shadowOf(app).grantPermissions(Manifest.permission.POST_NOTIFICATIONS)
        val manager = app.getSystemService(NotificationManager::class.java)
        manager.cancelAll()
        ShepherdDatabase(app).use { db ->
            db.activate(1_000)
            val rows = listOf(
                RecordingMetadata("pending", "Pending_261108_123456.m4a", 100, 10, 2_000),
                RecordingMetadata("processed", "Processed_261108_123456.m4a", 200, 10, 2_000))
            db.observe(rows, 3_000, ZoneId.of("UTC"))
            db.observe(rows, 33_000, ZoneId.of("UTC"))
            db.markProcessed("processed")
            ShepherdRuntime.notifyPending(app, db, 0)
        }
        val tapIntent = shadowOf(shadowOf(manager).allNotifications.single().contentIntent).savedIntent
        assertEquals(ShepherdRuntime.OPEN_PENDING, tapIntent.action)
        compose.onNodeWithText("내 데이터 흐름").assertExists()
        val originalIntent = compose.activity.intent
        try {
            // Deliver the actual notification intent to the existing activity's callback.
            compose.runOnUiThread {
                MainActivity::class.java.getDeclaredMethod("onNewIntent", Intent::class.java).apply {
                    isAccessible = true
                }.invoke(compose.activity, tapIntent)
            }
            compose.onNodeWithText("스크립트 미공유 통화").assertExists()
            compose.onNodeWithText("전체 통화 보기").assertExists()
            compose.onNodeWithText("처리됨").assertDoesNotExist()
            compose.waitUntil(5_000) {
                compose.onAllNodesWithText("처리 안 함").fetchSemanticsNodes().size == 1
            }
            compose.onNodeWithText("처리 안 함").performScrollTo().performClick()
            compose.waitUntil(5_000) { ShepherdRuntime.pendingCalls(app).isEmpty() }
            // The database can finish before the asynchronous screen refresh.
            compose.waitUntil(5_000) {
                compose.onAllNodesWithText("통화 없음").fetchSemanticsNodes().size == 1
            }
            compose.onNodeWithText("통화 없음").assertExists()
            assertTrue(shadowOf(manager).allNotifications.isEmpty())
        } finally {
            // ActivityScenario tracks the launch intent when closing the Robolectric activity.
            compose.runOnUiThread { compose.activity.intent = originalIntent }
        }
    }

    @Test
    fun permissionsFollowTheSdkSplit() {
        assertTrue(Feature.NOTIFICATIONS.permissions(sdk = 32).isEmpty())
        assertTrue(Feature.NOTIFICATIONS.permissions(sdk = 33) == listOf(Manifest.permission.POST_NOTIFICATIONS))
        assertTrue(Feature.SHEPHERD.permissions(sdk = 32) == listOf(Manifest.permission.READ_EXTERNAL_STORAGE))
        assertTrue(Feature.SHEPHERD.permissions(sdk = 36) == listOf(Manifest.permission.READ_MEDIA_AUDIO))
    }
}
