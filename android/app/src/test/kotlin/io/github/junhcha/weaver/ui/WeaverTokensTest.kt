package io.github.junhcha.weaver.ui

import java.io.File
import kotlinx.serialization.json.*
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Test
import javax.xml.parsers.DocumentBuilderFactory
import androidx.compose.ui.graphics.toArgb

class WeaverTokensTest {
    @Test fun androidResourcesMatchComposeColors() {
        val document = DocumentBuilderFactory.newInstance().newDocumentBuilder().parse(
            File(System.getProperty("weaver.resDir"), "values/colors.xml"))
        val nodes = document.getElementsByTagName("color")
        val resources = (0 until nodes.length).associate {
            val node = nodes.item(it)
            node.attributes.getNamedItem("name").nodeValue to node.textContent
        }
        val xmlPalette = mapOf("bg" to WeaverTokens.bg, "ink" to WeaverTokens.ink,
            "indigo" to WeaverTokens.indigo, "thread" to WeaverTokens.thread)
        assertEquals(xmlPalette.keys.map { "weaver_$it" }.toSet() + "ic_launcher_background", resources.keys)
        xmlPalette.forEach { (name, color) ->
            val hex = resources.getValue("weaver_$name").removePrefix("#").toLong(16) or 0xFF000000L
            assertEquals(hex.toInt(), color.toArgb())
        }
        assertEquals("@color/weaver_bg", resources["ic_launcher_background"])
    }

    @Test fun typedColorsAndCachedKindsMatchGeneratedValues() {
        val colors = mapOf("bg" to WeaverTokens.bg, "surface" to WeaverTokens.surface,
            "line" to WeaverTokens.line, "ink" to WeaverTokens.ink, "muted" to WeaverTokens.muted,
            "indigo" to WeaverTokens.indigo, "thread" to WeaverTokens.thread)
        colors.forEach { (name, color) ->
            val hex = WeaverTokens.values.getValue("color.$name").removePrefix("#").toLong(16) or 0xFF000000L
            assertEquals(hex.toInt(), color.toArgb())
        }
        listOf("hover" to WeaverTokens.hover, "selected" to WeaverTokens.selected).forEach { (name, color) ->
            val hex = WeaverTokens.values.getValue("color.$name.hex").removePrefix("#").toLong(16) or 0xFF000000L
            assertEquals((hex and 0xFFFFFF).toInt(), color.toArgb() and 0xFFFFFF)
            assertEquals(WeaverTokens.number("color.$name.alpha"), color.alpha, 0.002f)
        }
        listOf("request", "decision", "schedule", "record", "hollow").forEach { name ->
            val kind = WeaverTokens.kind(name)
            assertSame(kind, WeaverTokens.kind(name))
            val hex = WeaverTokens.values.getValue("color.kind.$name.hex").removePrefix("#").toLong(16) or 0xFF000000L
            assertEquals(hex.toInt(), kind.color.toArgb())
            assertEquals(WeaverTokens.number("color.kind.$name.tintPercent"), kind.tintPercent)
            assertEquals(WeaverTokens.values.getValue("color.kind.$name.band") == "dashed", kind.dashed)
        }
    }

    @Test fun generatedTokensMatchSource() {
        val source = Json.parseToJsonElement(File(System.getProperty("weaver.designDir"), "tokens.json").readText())
        fun flatten(value: JsonElement, path: String = ""): Map<String, String> = when (value) {
            is JsonObject -> value.filterKeys { !it.startsWith("$") }.flatMap { (key, child) ->
                flatten(child, if (path.isEmpty()) key else "$path.$key").entries.map { it.toPair() }
            }.toMap()
            is JsonArray -> value.flatMapIndexed { index, child -> flatten(child, "$path.$index").entries.map { it.toPair() } }.toMap()
            else -> mapOf(path to value.jsonPrimitive.content)
        }
        assertEquals("Run python3 android/scripts/generate_tokens.py", flatten(source), WeaverTokens.values)
    }
}
