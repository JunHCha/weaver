package io.github.junhcha.weaver.pipeline

import io.github.junhcha.weaver.schema.*
import java.nio.file.Files
import org.junit.Assert.*
import org.junit.Test

@org.junit.runner.RunWith(org.robolectric.RobolectricTestRunner::class)
class PipelineStoreTest {
    private fun item(revision: Long, location: PipelineLocation, history: List<PipelineLocation>) = PipelineItem(
        2, revision, "cap_01ARZ3NDEKTSV4RRFFQ69G5FAV", "src_01ARZ3NDEKTSV4RRFFQ69G5FAV", "package-1", 1,
        "call", "가상 항목", location, PipelineStatus.REVIEW, emptyMap(), history, emptyList(), emptyList(), null, 5, "2026-11-12T00:00:00Z",
    )
    @Test fun stalePartialAndConflictingSyncNeverEraseKnownProgress() {
        val dir = Files.createTempDirectory("pipeline").toFile()
        try {
            val store = PipelineStore(dir)
            val a = item(2, PipelineLocation.USER, listOf(PipelineLocation.SPINNER, PipelineLocation.LOOM, PipelineLocation.USER))
            store.merge("root", listOf(a, a.copy(revision=1)))
            store.merge("root", emptyList())
            store.merge("root", listOf(a.copy(revision = 1, location = PipelineLocation.SPINNER)))
            store.merge("root", listOf(a.copy(location = PipelineLocation.LOOM)))
            assertEquals(listOf(a), PipelineStore(dir).items("root"))
            assertTrue(store.items("other").isEmpty())
        } finally { dir.deleteRecursively() }
    }
    @Test fun storesMultipleHostsAndRetainsThemAcrossOldRevisions() {
        val dir = Files.createTempDirectory("pipeline-hosts").toFile()
        try {
            val store = PipelineStore(dir)
            val first = item(1, PipelineLocation.USER, listOf(PipelineLocation.USER))
            val latest = first.copy(revision = 2, captureHosts = listOf(CaptureHost.PHONE, CaptureHost.UNKNOWN))
            store.merge("root", listOf(first, latest, first))
            assertEquals(latest, PipelineStore(dir).items("root").single())
        } finally { dir.deleteRecursively() }
    }
    @Test fun visitUsesHistorySuffixIncludingFeedbackAndFirstVisitDoesNotInventMovement() {
        val a = item(1, PipelineLocation.USER, listOf(PipelineLocation.SPINNER, PipelineLocation.LOOM, PipelineLocation.USER))
        assertTrue(movement(null, a).isEmpty())
        val b = a.copy(revision = 4, history = a.history + listOf(PipelineLocation.LOOM, PipelineLocation.USER))
        assertEquals(listOf(PipelineLocation.USER, PipelineLocation.LOOM, PipelineLocation.USER), movement(a, b))
        assertTrue(movement(b, b).isEmpty())
    }
    @Test fun todaysDigestAvailabilityDoesNotDependOnModificationDay() {
        val today=java.time.LocalDate.of(2026,11,12)
        val digest=PipelinePage("wiki/Digest/2026-11-12.md","2026-11-11T00:00:00Z","a".repeat(64))
        val historical=digest.copy(path="wiki/Digest/2026-11-11.md",updatedAt="2026-11-12T00:00:00Z")
        val value=item(1,PipelineLocation.WIKI,listOf(PipelineLocation.WIKI))
        assertEquals(listOf(digest),io.github.junhcha.weaver.ui.todayResults(listOf(value.copy(digest=digest)),today))
        assertTrue(io.github.junhcha.weaver.ui.todayResults(listOf(value.copy(digest=historical)),today).isEmpty())
    }
    @Test fun onlySafeVaultPathsAndValidEnumsAreAccepted() {
        assertTrue(runCatching { PipelinePage("../outside.md", "2026-11-12T00:00:00Z", "a".repeat(64)) }.isFailure)
        val a = item(1, PipelineLocation.USER, listOf(PipelineLocation.USER))
        assertTrue(WeaverCodec.decode<PipelineItem>(WeaverCodec.encode(a)) is DecodeResult.Ok)
        assertTrue(WeaverCodec.decode<PipelineItem>(WeaverCodec.encode(a).replace("\"review\"", "\"unknown\"")) is DecodeResult.Invalid)
    }
    @Test fun wikiEditOutputsAreCheckedForArrivalEvenOnLaterDays() {
        val wiki = WeaverCodec.json.decodeFromString<PipelineItem>(java.io.File(System.getProperty("weaver.schemaDir"), "examples/PipelineItem-wiki-edit.json").readText())
        assertEquals(wiki.pages, io.github.junhcha.weaver.ui.panelResultPages(listOf(wiki), java.time.LocalDate.of(2026, 12, 1)))
    }

    @Test fun olderCaptureObservationWithoutAddedDefaultsStillDecodes() {
        val source = java.io.File(System.getProperty("weaver.schemaDir"), "examples/PipelineItem.json").readText()
        val root = kotlinx.serialization.json.Json.parseToJsonElement(source) as kotlinx.serialization.json.JsonObject
        val old = kotlinx.serialization.json.JsonObject(root - "wiki_edit")
        assertTrue(WeaverCodec.decode<PipelineItem>(old.toString()) is DecodeResult.Ok)
    }

}
