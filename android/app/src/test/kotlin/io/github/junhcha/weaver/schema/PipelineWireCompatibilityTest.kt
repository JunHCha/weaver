package io.github.junhcha.weaver.schema

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Existing strict Android readers must never receive a counterpart Thread key. */
class PipelineWireCompatibilityTest {
    @Test
    fun pipelineWireOmitsCounterpartWithAndWithoutWikiEdit() {
        for (suffix in listOf("PipelineItem.json", "PipelineItem-wiki-edit.json")) {
            val text = SharedSchema.examples("PipelineItem").single { it.name == suffix }.readText()
            val result = WeaverCodec.decode<PipelineItem>(text)
            assertTrue(result is DecodeResult.Ok)
            val encoded = WeaverCodec.encode((result as DecodeResult.Ok).value)
            assertFalse(encoded.contains("\"counterpart\""))
            assertTrue(SharedSchema.errors("PipelineItem", encoded).isEmpty())
        }
    }
}
