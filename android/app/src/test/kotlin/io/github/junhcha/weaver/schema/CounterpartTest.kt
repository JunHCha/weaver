package io.github.junhcha.weaver.schema

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The phone's Thread projection carries the same optional scalar party as Fabric. */
class CounterpartTest {
    @Test
    fun legacyAndCanonicalCounterpartMirrorExportedSchema() {
        val base = Json.parseToJsonElement(SharedSchema.examples("PipelineItem").first().readText()).jsonObject
        fun text(party: kotlinx.serialization.json.JsonElement?): String {
            val thread = mutableMapOf("thread_id" to JsonPrimitive("synthetic"), "kind" to JsonPrimitive("기록"), "content" to JsonPrimitive("합성"))
                .mapValues { it.value as kotlinx.serialization.json.JsonElement }.toMutableMap()
            if (party != null) thread["counterpart"] = party
            return JsonObject(base + ("threads" to JsonArray(listOf(JsonObject(thread))))).toString()
        }
        for (party in listOf(null, JsonNull, JsonPrimitive("김철수"))) {
            val json = text(party)
            assertTrue(SharedSchema.errors("PipelineItem", json).isEmpty())
            val result = WeaverCodec.decode<PipelineItem>(json)
            assertTrue(result is DecodeResult.Ok)
            val thread = (result as DecodeResult.Ok).value.threads.single()
            if (party is JsonPrimitive && party != JsonNull) assertEquals("김철수", thread.counterpart)
            else assertNull(thread.counterpart)
        }
        for (party in listOf(JsonPrimitive(" "), JsonArray(listOf(JsonPrimitive("김철수"))))) {
            val json = text(party)
            assertFalse(SharedSchema.errors("PipelineItem", json).isEmpty())
            assertTrue(WeaverCodec.decode<PipelineItem>(json) is DecodeResult.Invalid)
        }
    }
}
