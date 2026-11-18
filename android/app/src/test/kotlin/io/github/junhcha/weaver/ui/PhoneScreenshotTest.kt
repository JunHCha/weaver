package io.github.junhcha.weaver.ui

import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import io.github.junhcha.weaver.phone.StatusCounts
import io.github.junhcha.weaver.phone.SyntheticPhoneReadModel
import java.io.File
import java.time.Clock
import java.time.OffsetDateTime
import java.time.ZoneId
import org.junit.Assume.assumeTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

/**
 * Renders each phone screen to a PNG when `WEAVER_SCREENSHOT_DIR` is set; otherwise it is skipped.
 * Run: `WEAVER_SCREENSHOT_DIR=/some/dir ./gradlew testDebugUnitTest --tests '*PhoneScreenshotTest'`.
 */
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [36], qualifiers = "w360dp-h740dp-xxhdpi")
class PhoneScreenshotTest {
    @get:Rule val compose = createAndroidComposeRule<androidx.activity.ComponentActivity>()

    private val now = OffsetDateTime.parse("2026-11-18T09:00:00+09:00").toInstant()

    private fun shoot(name: String) {
        val dir = File(System.getenv("WEAVER_SCREENSHOT_DIR")!!).apply { mkdirs() }
        val view = compose.activity.window.decorView
        val bitmap = Bitmap.createBitmap(view.width, view.height, Bitmap.Config.ARGB_8888)
        view.draw(android.graphics.Canvas(bitmap))
        File(dir, "$name.png").outputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) }
    }

    @Test fun phoneScreens() {
        assumeTrue(System.getenv("WEAVER_SCREENSHOT_DIR") != null)
        compose.activity.setContent {
            WeaverTheme {
                androidx.compose.material3.Surface {
                    PhoneShell(SyntheticPhoneReadModel(Clock.fixed(now, ZoneId.of("UTC"))), StatusCounts(2, 1, 0), { now }, {}, {})
                }
            }
        }
        compose.waitForIdle()
        shoot("1-now")
        compose.onNodeWithTag("tab-PEOPLE").performClick()
        compose.waitForIdle(); shoot("2a-people-list")
        compose.onNodeWithText("이서준").performClick()
        compose.waitForIdle(); shoot("2b-person-timeline")
        compose.onNodeWithText("언급 포함").performClick()
        compose.waitForIdle(); shoot("2c-person-timeline-mentions")
        compose.onNodeWithText("이력 포함").performClick()
        compose.waitForIdle(); shoot("2d-person-timeline-history")
        compose.onNodeWithTag("tab-AREAS").performClick()
        compose.waitForIdle(); shoot("2e-area-list")
        compose.onNodeWithTag("tab-WIKI").performClick()
        compose.waitForIdle(); shoot("3a-wiki-list")
        compose.onNodeWithText("방화벽 장비 교체").performClick()
        compose.waitForIdle(); shoot("3b-wiki-viewer")
    }

    @Test fun releaseEmptyState() {
        assumeTrue(System.getenv("WEAVER_SCREENSHOT_DIR") != null)
        compose.activity.setContent {
            WeaverTheme {
                androidx.compose.material3.Surface {
                    PhoneShell(io.github.junhcha.weaver.phone.PhoneReadModels.forBuild(debug = false), StatusCounts(0, 0, 0), { now }, {}, {})
                }
            }
        }
        compose.waitForIdle()
        shoot("4-release-no-data")
    }
}
