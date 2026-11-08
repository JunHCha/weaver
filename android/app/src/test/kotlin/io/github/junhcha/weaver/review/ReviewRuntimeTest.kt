package io.github.junhcha.weaver.review

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import io.github.junhcha.weaver.courier.*
import io.github.junhcha.weaver.schema.*
import io.github.junhcha.weaver.storage.AppStorage
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class ReviewRuntimeTest {
    @Test fun courierWorkerPublishesDurableReviewWithoutDeletingRemoteFiles() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        File(AppStorage.exchangeDir(context), "reviews").deleteRecursively()
        File(context.filesDir, "inbox.sqlite3").delete()
        val p = CourierClient.decoded<Proposal>(File(System.getProperty("weaver.schemaDir"), "examples/Proposal.json").readText())
        val files = mutableMapOf("proposals/${p.proposalId}.r${p.rev}.json" to WeaverCodec.encode(p))
        var denied = false
        var publishes = 0
        val folder = object : CourierFolder {
            override val identity = "synthetic-review-worker"
            override fun list(folder: String): List<CourierEntry> {
                if (denied) error("synthetic_listing_error")
                return files.keys.filter { it.startsWith("$folder/") }.map { CourierEntry(it.substringAfter('/'), 0) }
            }
            override fun read(folder: String, name: String) = files.getValue("$folder/$name").byteInputStream()
            override fun publish(folder: String, name: String, write: (java.io.OutputStream) -> Unit) {
                val out = java.io.ByteArrayOutputStream(); write(out)
                check("$folder/$name" !in files)
                files["$folder/$name"] = out.toString("UTF-8"); publishes++
            }
            override fun delete(folder: String, name: String): Unit = error("unexpected remote deletion")
        }
        val verdict = Verdict(1, p.proposalId, p.rev, Verdict.Kind.REVISE, emptyList(), null,
            "Synthetic instruction", "2026-11-08T00:00:00Z")
        val store = ReviewStore(File(AppStorage.exchangeDir(context), "reviews"))
        store.queue(folder, p, verdict)
        assertEquals(p, ReviewStore(File(AppStorage.exchangeDir(context), "reviews")).records().single().proposal)
        CourierDatabase(context).use { db -> assertTrue(CourierWorker.synchronize(context, folder, db)) }
        assertEquals(ReviewStatus.SENT, ReviewRuntime.records(context).single().status)
        assertEquals(1, publishes)
        denied = true
        assertFalse(ReviewRuntime.synchronize(context, folder))
        assertTrue(ReviewRuntime.records(context).single().published)
        denied = false
        files.remove("decisions/${p.proposalId}.r${p.rev}.json")
        assertTrue(ReviewRuntime.synchronize(context, folder))
        assertEquals(ReviewStatus.INFERRED, ReviewRuntime.records(context).single().status)
        assertEquals(1, publishes)
        assertTrue(files.containsKey("proposals/${p.proposalId}.r${p.rev}.json"))
    }
}
