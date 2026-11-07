package io.github.junhcha.weaver.ui

import android.Manifest
import android.app.Application
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import io.github.junhcha.weaver.MainActivity
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

        compose.onNodeWithText("Weaver").assertExists()
        compose.onAllNodesWithText("허용됨").assertCountEquals(2)
        compose.onAllNodesWithText("권한 없음").assertCountEquals(1)
    }

    @Test
    fun permissionsFollowTheSdkSplit() {
        assertTrue(Feature.NOTIFICATIONS.permissions(sdk = 32).isEmpty())
        assertTrue(Feature.NOTIFICATIONS.permissions(sdk = 33) == listOf(Manifest.permission.POST_NOTIFICATIONS))
        assertTrue(Feature.SHEPHERD.permissions(sdk = 32) == listOf(Manifest.permission.READ_EXTERNAL_STORAGE))
        assertTrue(Feature.SHEPHERD.permissions(sdk = 36) == listOf(Manifest.permission.READ_MEDIA_AUDIO))
    }
}
