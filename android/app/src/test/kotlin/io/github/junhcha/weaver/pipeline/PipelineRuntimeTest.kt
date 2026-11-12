package io.github.junhcha.weaver.pipeline

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import io.github.junhcha.weaver.courier.*
import io.github.junhcha.weaver.schema.*
import java.io.File
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class PipelineRuntimeTest {
    @Test fun matchedReceiptSurvivesRemovalAndCanonicalSourceDeduplicatesCapture() {
        val context=ApplicationProvider.getApplicationContext<Context>()
        val identity="content://test/root"
        context.getSharedPreferences("courier",Context.MODE_PRIVATE).edit().putString("tree",identity).commit()
        val manifest=WeaverCodec.json.decodeFromString<Manifest>(File(System.getProperty("weaver.schemaDir"),"examples/Manifest.json").readText())
        CourierDatabase(context).use { db -> db.prepare("content://test/source",manifest,File(context.filesDir,"fake.txt")) }
        val receipt=Receipt(1,manifest.captureId,"src_01ARZ3NDEKTSV4RRFFQ69G5FAV",manifest.sha256,"2026-11-12T01:00:00Z")
        val empty=CourierClient.Inbox(emptyList(),emptyList(),emptyList())
        PipelineRuntime.observe(context,identity,empty.copy(receipts=listOf(receipt.copy(sha256=if(manifest.sha256=="a".repeat(64))"b".repeat(64)else"a".repeat(64)))))
        assertEquals(MapNode.SHEPHERD,PipelineRuntime.snapshot(context).single().node)
        PipelineRuntime.observe(context,identity,empty.copy(receipts=listOf(receipt)))
        PipelineRuntime.observe(context,identity,empty)
        assertEquals(MapNode.SPINNER,PipelineRuntime.snapshot(context).single().node)
        val item=PipelineItem(2,1,manifest.captureId,receipt.sourceId,null,null,"call","가상 항목",PipelineLocation.SPINNER,PipelineStatus.WORKING,emptyMap(),listOf(PipelineLocation.SPINNER),emptyList(),emptyList(),null,5,receipt.receivedAt)
        PipelineRuntime.observe(context,identity,empty.copy(pipeline=listOf(item)))
        assertEquals(1,PipelineRuntime.snapshot(context).size)
        val first=PipelineRuntime.snapshot(context)
        assertTrue(PipelineRuntime.visit(context,first).isEmpty())
        val second=first.map { it.copy(node=MapNode.USER,server=item.copy(revision=2,location=PipelineLocation.USER,history=item.history+listOf(PipelineLocation.LOOM,PipelineLocation.USER))) }
        assertEquals(listOf(MapNode.SPINNER,MapNode.LOOM,MapNode.USER),PipelineRuntime.visit(context,second).values.single())
        assertTrue(PipelineRuntime.visit(context,second).isEmpty())
    }
}
