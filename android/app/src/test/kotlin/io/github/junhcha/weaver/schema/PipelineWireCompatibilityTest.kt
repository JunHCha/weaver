package io.github.junhcha.weaver.schema

import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertFalse
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** Existing strict Android readers must never receive a counterpart Thread key. */
class PipelineWireCompatibilityTest {
    @Test
    fun pipelineWireKeepsCounterpartAtCardLevelOnly() {
        for (suffix in listOf("PipelineItem.json", "PipelineItem-wiki-edit.json")) {
            val text = SharedSchema.examples("PipelineItem").single { it.name == suffix }.readText()
            val result = WeaverCodec.decode<PipelineItem>(text)
            assertTrue(result is DecodeResult.Ok)
            val encoded = WeaverCodec.encode((result as DecodeResult.Ok).value)
            val wire = WeaverCodec.json.parseToJsonElement(encoded).jsonObject
            wire.getValue("threads").jsonArray.forEach { assertFalse(it.jsonObject.containsKey("counterpart")) }
            assertTrue(SharedSchema.errors("PipelineItem", encoded).isEmpty())
        }
    }
    @Test fun oldFilesDecodeWithEmptyMetadataAndNewMetadataSurvivesRoundTrip() {
        val text = SharedSchema.examples("PipelineItem").single { it.name == "PipelineItem.json" }.readText()
        val fields = WeaverCodec.json.parseToJsonElement(text).jsonObject.toMutableMap()
        listOf("summary", "counterpart", "related_tags").forEach(fields::remove)
        val old = WeaverCodec.json.decodeFromString<PipelineItem>(kotlinx.serialization.json.JsonObject(fields).toString())
        assertEquals("", old.summary)
        assertEquals(null, old.counterpart)
        assertEquals(emptyList<PipelineTag>(), old.relatedTags)
        val updated = old.copy(summary = "검토할 검색 시안", counterpart = "민수", relatedTags = listOf(PipelineTag("검색", "area"), PipelineTag("미분류")))
        val encoded = WeaverCodec.encode(updated)
        assertEquals(updated, WeaverCodec.json.decodeFromString<PipelineItem>(encoded))
        assertTrue(SharedSchema.errors("PipelineItem", encoded).isEmpty())
    }
}
