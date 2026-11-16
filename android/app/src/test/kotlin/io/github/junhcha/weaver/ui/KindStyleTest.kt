package io.github.junhcha.weaver.ui

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import org.junit.Assert.*
import org.junit.Test

class KindStyleTest {
    @Test fun threadKindsUseTheirOwnBandAndTint() {
        mapOf("요청" to "request", "결정" to "decision", "일정" to "schedule", "기록" to "record").forEach { (label, name) ->
            assertEquals(WeaverTokens.kind(name), threadKindStyle(label))
            assertFalse(threadKindStyle(label).dashed)
            assertNotEquals(Color.Transparent, kindBackground(threadKindStyle(label)))
        }
    }

    @Test fun hollowKindsHaveDashedBandWithoutBackground() {
        listOf("태그정정", "거절", "실행").forEach {
            val kind = threadKindStyle(it)
            assertEquals(WeaverTokens.hollow, kind)
            assertTrue(kind.dashed)
            assertEquals(Color.Transparent, kindBackground(kind))
        }
    }

    @Test fun tintMixesSrgbChannelsLikeWebColorMix() {
        val actual = kindBackground(WeaverTokens.request).toArgb()
        val foreground = WeaverTokens.request.color.toArgb()
        val surface = WeaverTokens.surface.toArgb()
        for (shift in listOf(0, 8, 16)) {
            val expected = (((foreground shr shift) and 255) * .06f + ((surface shr shift) and 255) * .94f)
            assertEquals(expected, ((actual shr shift) and 255).toFloat(), 1f)
        }
    }
}
