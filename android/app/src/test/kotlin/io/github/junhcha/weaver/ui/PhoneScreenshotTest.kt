package io.github.junhcha.weaver.ui

import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
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
        compose.onNodeWithTag("tab-LOG").performClick()
        compose.waitForIdle(); shoot("2-log-all")
        compose.onNodeWithTag("log-filter-PEOPLE").performClick()
        compose.waitForIdle(); shoot("2a-people-list")
        compose.onNodeWithText("이서준").performClick()
        compose.waitForIdle(); shoot("2b-person-timeline")
        compose.onNodeWithText("언급 포함").performClick()
        compose.waitForIdle(); shoot("2c-person-timeline-mentions")
        compose.onNodeWithText("이력 포함").performClick()
        compose.waitForIdle(); shoot("2d-person-timeline-history")
        compose.onNodeWithTag("tab-AREAS").performClick()
        compose.waitForIdle(); shoot("2e-areas-and-documents")
        compose.onNodeWithText("방화벽 장비 교체").performClick()
        compose.waitForIdle(); shoot("3b-wiki-viewer")
    }

    /** Pairing, connection status and the tag edit sheet (content drawn over the timeline like the bottom sheet). */
    @Test fun transportScreens() {
        assumeTrue(System.getenv("WEAVER_SCREENSHOT_DIR") != null)
        val synthetic = kotlinx.coroutines.runBlocking { SyntheticPhoneReadModel(Clock.fixed(now, ZoneId.of("UTC"))).snapshot() }
        val syncedAt = now.minusSeconds(14 * 60).toEpochMilli()
        val shown = androidx.compose.runtime.mutableStateOf(0)
        compose.activity.setContent {
            WeaverTheme {
                androidx.compose.material3.Surface {
                    androidx.compose.foundation.layout.Column(androidx.compose.ui.Modifier.padding(16.dp)) {
                        when (shown.value) {
                            0 -> ConnectionContent(ConnectionUi(), false, null, {}, {}, {}, {})
                            1 -> ConnectionContent(
                                ConnectionUi(true, "https://100.106.199.83:8787", false, syncedAt, null,
                                    io.github.junhcha.weaver.transport.QueueCounts(2, 1, 0, 0),
                                    mapOf(
                                        io.github.junhcha.weaver.transport.TransportFeature.CAPTURE to io.github.junhcha.weaver.transport.TransportRoute.HTTP,
                                        io.github.junhcha.weaver.transport.TransportFeature.VERDICT to io.github.junhcha.weaver.transport.TransportRoute.HTTP,
                                        io.github.junhcha.weaver.transport.TransportFeature.ACTION_RESULT to io.github.junhcha.weaver.transport.TransportRoute.FILES,
                                    )),
                                false, null, {}, {}, {}, {})
                            2 -> ConnectionContent(
                                ConnectionUi(true, "https://100.106.199.83:8787", false, syncedAt, "unreachable",
                                    io.github.junhcha.weaver.transport.QueueCounts(3, 1, 1, 0)),
                                false, null, {}, {}, {}, {})
                        }
                    }
                }
            }
        }
        compose.waitForIdle(); shoot("5a-pairing")
        shown.value = 1; compose.waitForIdle(); shoot("5b-connection-status")
        shown.value = 2; compose.waitForIdle(); shoot("5c-connection-waiting")

        val fact = synthetic.threads.first { it.id == "t-12" }
        val other = synthetic.threads.first { it.id == "t-10" }
        val single = androidx.compose.runtime.mutableStateOf(true)
        compose.activity.setContent {
            WeaverTheme {
                androidx.compose.material3.Surface {
                    androidx.compose.foundation.layout.Box(androidx.compose.ui.Modifier.fillMaxSize()) {
                        TimelineScreen(synthetic, "이서준", {}, RecordingTagHost())
                        androidx.compose.material3.Surface(
                            Modifier.align(androidx.compose.ui.Alignment.BottomCenter).fillMaxWidth(),
                            shape = androidx.compose.foundation.shape.RoundedCornerShape(topStart = 28.dp, topEnd = 28.dp),
                            shadowElevation = 12.dp, color = WeaverTokens.surface,
                        ) {
                            androidx.compose.foundation.layout.Column(Modifier.padding(top = 16.dp)) {
                                TagEditContent(
                                    if (single.value) listOf(fact) else listOf(fact, other), synthetic.tags,
                                    listOf(io.github.junhcha.weaver.phone.DictionaryEntry("방화벽 장비 교체", io.github.junhcha.weaver.phone.TagKind.PROJECT, listOf("fw"), 3)),
                                    false, null, {}, { _, _, _ -> },
                                )
                            }
                        }
                    }
                }
            }
        }
        compose.waitForIdle()
        compose.onNodeWithTag("tag-search").performTextInput("방화")
        compose.waitForIdle(); shoot("6a-tag-edit-sheet")
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
