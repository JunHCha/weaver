package io.github.junhcha.weaver.ui

import java.io.File
import kotlinx.serialization.json.*
import org.junit.Assert.assertEquals
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
        WeaverTokens.values.filter { (path, _) -> path.startsWith("color.") && path.count { it == '.' } == 1 }
            .forEach { (path, hex) -> assertEquals(hex, resources["weaver_${path.substringAfter('.')}"]) }
        listOf("request", "decision", "schedule", "record", "hollow").forEach { kind ->
            val hex = resources.getValue("weaver_$kind").removePrefix("#").toLong(16) or 0xFF000000L
            assertEquals(hex.toInt(), WeaverTokens.kind(kind).color.toArgb())
        }
        assertEquals("@color/weaver_bg", resources["ic_launcher_background"])
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
