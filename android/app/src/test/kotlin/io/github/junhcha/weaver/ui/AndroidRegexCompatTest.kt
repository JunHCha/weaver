package io.github.junhcha.weaver.ui

import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit tests run on the desktop JVM regex engine, which accepts flags Android's ICU engine rejects at runtime
 * (`(?U)` threw PatternSyntaxException on device and crashed the map, #178). Guard the source instead.
 */
class AndroidRegexCompatTest {
    @Test
    fun appSourcesUseNoIcuUnsupportedInlineFlags() {
        val root = File("src/main/kotlin")
        val offenders = root.walkTopDown()
            .filter { it.isFile && it.extension == "kt" }
            .filter { file -> UNSUPPORTED.any { file.readText().contains(it) } }
            .map { it.relativeTo(root).path }
            .toList()
        assertTrue("ICU-unsupported regex flags in: $offenders", offenders.isEmpty())
    }

    private companion object {
        val UNSUPPORTED = listOf("(?U)", "Pattern.UNICODE_CHARACTER_CLASS", "RegexOption.UNICODE_CHARACTER_CLASS")
    }
}
