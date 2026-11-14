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
        assertEquals(listOf("수집: 이 폰"), PipelineRuntime.snapshot(context).single().captureLabels)
        PipelineRuntime.observe(context,identity,empty.copy(receipts=listOf(receipt)))
        PipelineRuntime.observe(context,identity,empty)
        assertEquals(MapNode.SPINNER,PipelineRuntime.snapshot(context).single().node)
        val item=PipelineItem(2,1,manifest.captureId,receipt.sourceId,null,null,"call","가상 항목",PipelineLocation.SPINNER,PipelineStatus.WORKING,emptyMap(),listOf(PipelineLocation.SPINNER),emptyList(),emptyList(),null,5,receipt.receivedAt)
        PipelineRuntime.observe(context,identity,empty.copy(pipeline=listOf(item.copy(captureHosts = listOf(CaptureHost.MAC, CaptureHost.UNKNOWN)))))
        assertEquals(1,PipelineRuntime.snapshot(context).size)
        assertEquals(listOf("수집: Mac", "수집: 알 수 없음"), PipelineRuntime.snapshot(context).single().captureLabels)
        val first=PipelineRuntime.snapshot(context)
        assertTrue(PipelineRuntime.visit(context,first).isEmpty())
        val second=first.map { it.copy(node=MapNode.USER,server=item.copy(revision=2,location=PipelineLocation.USER,history=item.history+listOf(PipelineLocation.LOOM,PipelineLocation.USER))) }
        assertEquals(listOf(MapNode.SPINNER,MapNode.LOOM,MapNode.USER),PipelineRuntime.visit(context,second).values.single())
        assertTrue(PipelineRuntime.visit(context,second).isEmpty())
    }
    @Test fun wikiEditHasNoCaptureAndReplaysOnlyWikiFabricWiki() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val identity = "content://test/wiki-origin"
        context.getSharedPreferences("courier", Context.MODE_PRIVATE).edit().putString("tree", identity).commit()
        val done = WeaverCodec.json.decodeFromString<PipelineItem>(File(System.getProperty("weaver.schemaDir"), "examples/PipelineItem-wiki-edit.json").readText())
        val waiting = done.copy(revision = 1, location = PipelineLocation.WIKI, status = PipelineStatus.WORKING,
            history = listOf(PipelineLocation.WIKI), stageTimes = emptyMap(), threads = emptyList(), pages = emptyList(),
            wikiEdit = done.wikiEdit!!.copy(state = "settling"))
        val inbox = CourierClient.Inbox(emptyList(), emptyList(), emptyList())
        PipelineRuntime.observe(context, identity, inbox.copy(pipeline = listOf(waiting)))
        val first = PipelineRuntime.snapshot(context)
        assertEquals(done.sourceId, first.single().id)
        assertEquals("Wiki 직접 수정", first.single().source)
        assertFalse(first.single().needsReview)
        assertNull(first.single().server!!.captureId)
        assertTrue(PipelineRuntime.visit(context, first).isEmpty())
        PipelineRuntime.observe(context, identity, inbox.copy(pipeline = listOf(done.copy(revision = 2))))
        val updated = PipelineRuntime.snapshot(context)
        assertEquals(listOf(MapNode.WIKI, MapNode.FABRIC, MapNode.WIKI), PipelineRuntime.visit(context, updated).values.single())
        assertTrue(PipelineRuntime.visit(context, updated).isEmpty())
        assertEquals("Wiki 갱신 완료", updated.single().stateLabel)
    }

}
