package io.github.junhcha.weaver.courier

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import io.github.junhcha.weaver.schema.*
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File
import java.io.InputStream
import java.io.OutputStream

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class CourierClientTest {
    private lateinit var context: Context
    private lateinit var db: CourierDatabase
    private val folder = MemoryFolder()
    private val id = "cap_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    private val secondId = "cap_01ARZ3NDEKTSV4RRFFQ69G5FAW"
    private val bytes = "synthetic text".toByteArray()
    private val sha = CourierClient.digest(bytes.inputStream()).first
    private val manifest get() = Manifest(1, id, CaptureKind.TEXT, Channel.SHARE_TEXT,
        "$id.txt", sha, bytes.size.toLong(), "2026-11-07T00:00:00Z", null, Manifest.Hints(null))
    private val receipt get() = Receipt(1, id, "src_01ARZ3NDEKTSV4RRFFQ69G5FAV", sha, "2026-11-07T00:00:00Z")

    @Before fun setup() {
        context = ApplicationProvider.getApplicationContext()
        File(context.filesDir, "courier.sqlite3").delete()
        db = CourierDatabase(context)
    }

    private fun prepare(m: Manifest = manifest, uri: String = "content://test/source"): CourierDatabase.Capture {
        val file = File(context.filesDir, "staged-${m.captureId}").apply { writeBytes(bytes) }
        return db.prepare(uri, m, file)
    }
    private fun addReceipt(r: Receipt = receipt) { folder.put("receipts", "$id.json", WeaverCodec.encode(r)) }

    @Test fun reconcileExceptionDoesNotSkipQueuedVerdictsAndRequestsRetry() {
        val previous = io.github.junhcha.weaver.shepherd.PendingCalls.repository
        val callManifest = manifest.copy(channel = Channel.CALL)
        prepare(callManifest)
        context.getSharedPreferences("shepherd-call-links", Context.MODE_PRIVATE).edit()
            .putString(id, "synthetic-call").commit()
        io.github.junhcha.weaver.shepherd.PendingCalls.repository = {
            error("synthetic_reconcile_failure")
        }
        val verdict = Verdict(2, "pkg_0123456789abcdef", 1, Verdict.Action.APPROVE,
            emptyList(), null, null)
        val queued = File(context.filesDir, "courier-verdicts").apply { mkdirs() }
        val name = "${verdict.packageId}.r1.json"
        File(queued, name).writeText(WeaverCodec.encode(verdict))
        try {
            assertFalse(CourierWorker.synchronize(context, folder, db))
            assertTrue(folder.files.containsKey("decisions/$name"))
            assertFalse(File(queued, name).exists())
            assertTrue(db.diagnostics().any { it.contains("shepherd/reconcile") })
        } finally {
            io.github.junhcha.weaver.shepherd.PendingCalls.repository = previous
            context.getSharedPreferences("shepherd-call-links", Context.MODE_PRIVATE).edit().clear().commit()
        }
    }

    @Test fun failedManifestRetriesWithSameIdAndBodyAfterDatabaseRestart() {
        prepare()
        folder.failName = "$id.json"
        assertFalse(CourierClient(folder, db).synchronize())
        assertArrayEquals(bytes, folder.files.getValue("outbox/${manifest.payload}"))
        assertFalse(folder.files.containsKey("outbox/$id.json"))
        db.close()
        db = CourierDatabase(context)
        assertEquals(id, db.captures().single().manifest.captureId)
        folder.failName = null
        assertTrue(CourierClient(folder, db).synchronize())
        assertEquals(listOf(manifest.payload, "$id.json"), folder.published)
        assertEquals("SENT", db.captures().single().state)
        assertTrue(CourierClient(folder, db).synchronize())
        assertEquals(2, folder.published.size)
    }

    @Test fun sameHashReusesCaptureAndUriMutationIsRejected() {
        prepare()
        assertEquals(id, prepare(manifest.copy(captureId = secondId, payload = "$secondId.txt"), "content://test/other").manifest.captureId)
        assertEquals(1, db.captures().size)
        assertThrows(IllegalStateException::class.java) {
            prepare(manifest.copy(sha256 = "0".repeat(64)))
        }
    }

    @Test fun existingDifferentBodyNeverPublishesManifest() {
        prepare()
        folder.put("outbox", manifest.payload, "wrong")
        assertFalse(CourierClient(folder, db).synchronize())
        assertFalse(folder.files.containsKey("outbox/$id.json"))
        assertEquals("wrong", folder.files.getValue("outbox/${manifest.payload}").decodeToString())
    }

    @Test fun editedStagingNeverPublishesBodyOrManifest() {
        val capture = prepare()
        File(capture.stagedPath).writeText("changed")
        assertFalse(CourierClient(folder, db).synchronize())
        assertTrue(folder.published.isEmpty())
    }

    @Test fun receiptMustMatchHashAndCleanupShipsOff() {
        prepare()
        CourierClient(folder, db).synchronize()
        addReceipt(receipt.copy(sha256 = "0".repeat(64)))
        CourierClient(folder, db).synchronize()
        assertEquals("SENT", db.captures().single().state)
        addReceipt()
        CourierClient(folder, db, now = { Long.MAX_VALUE / 2 }).synchronize()
        assertEquals("RECEIVED", db.captures().single().state)
        assertTrue(folder.deleted.isEmpty())
        assertTrue(db.diagnostics().any { "hash_mismatch" in it })
    }

    @Test fun cleanupWaitsBetweenStepsAndDeletesResurrectedDoneReceiptOnly() {
        val capture = prepare()
        var now = 10_000_000L
        val client = CourierClient(folder, db, true) { now }
        client.synchronize()
        addReceipt()
        client.synchronize()
        client.synchronize()
        assertTrue(folder.deleted.isEmpty())
        repeat(3) {
            now += CourierRules.cleanupWait
            client.synchronize()
            assertEquals(it + 1, folder.deleted.size)
            client.synchronize()
            assertEquals(it + 1, folder.deleted.size)
        }
        assertEquals(listOf("outbox/${manifest.payload}", "outbox/$id.json", "receipts/$id.json"), folder.deleted)
        assertEquals("DONE", db.captures().single().state)
        addReceipt()
        client.synchronize()
        assertEquals("receipts/$id.json", folder.deleted.last())
        assertTrue(File(capture.stagedPath).exists())
    }

    @Test fun scanSkipsInvalidFilesAndContinuesToValidReceipt() {
        folder.put("receipts", ".ignored.json", "bad")
        folder.put("receipts", "~syncthing~temp", "bad")
        folder.put("receipts", "$id.sync-conflict-20261107.json", "bad")
        folder.put("receipts", "$secondId.json", "{\"schema_version\":2}")
        folder.put("receipts", "cap_01ARZ3NDEKTSV4RRFFQ69G5FAX.json", "{")
        folder.put("receipts", "cap_01ARZ3NDEKTSV4RRFFQ69G5FAY.json", "x".repeat(CourierRules.maxJson + 1))
        folder.put("receipts", "../outside.json", "bad")
        addReceipt()
        val inbox = CourierClient(folder, db).scan()
        assertEquals(listOf(receipt), inbox.receipts)
        val diagnostics = db.diagnostics().joinToString()
        assertTrue(diagnostics.contains("sync_conflict"))
        assertTrue(diagnostics.contains("unknown_version"))
        assertTrue(diagnostics.contains("invalid_schema"))
        assertTrue(diagnostics.contains("too_large"))
        assertFalse(diagnostics.contains(".ignored.json"))
        assertTrue(folder.deleted.isEmpty())
    }

    @Test fun packagesKeepHighestRevisionAndAlertsUseTheirOwnFilenameContract() {
        val first = WeaverCodec.json.decodeFromString<ReviewPackage>(
            File(io.github.junhcha.weaver.schema.SharedSchema.dir, "examples/ReviewPackage.json").readText(),
        )
        val latest = first.copy(revision = first.revision + 1)
        val alert = WeaverCodec.json.decodeFromString<Alert>(
            File(io.github.junhcha.weaver.schema.SharedSchema.dir, "examples/Alert.json").readText(),
        )
        folder.put("proposals", "${latest.packageId}.r${latest.revision}.json", WeaverCodec.encode(latest))
        folder.put("proposals", "${first.packageId}.r${first.revision}.json", WeaverCodec.encode(first))
        folder.put("proposals", "alert_${alert.alertId}.json", WeaverCodec.encode(alert))
        val inbox = CourierClient(folder, db).scan()
        assertEquals(listOf(latest), inbox.packages)
        assertEquals(listOf(alert), inbox.alerts)
    }

    @Test fun packagesOfTheOlderContractAreReportedNotInterpreted() {
        val first = WeaverCodec.json.decodeFromString<ReviewPackage>(
            File(io.github.junhcha.weaver.schema.SharedSchema.dir, "examples/ReviewPackage.json").readText(),
        )
        val name = "${first.packageId}.r1.json"
        folder.put("proposals", name, WeaverCodec.encode(first).replace("\"schema_version\":2", "\"schema_version\":1"))
        val inbox = CourierClient(folder, db).scan()
        assertTrue(inbox.packages.isEmpty())
        assertEquals(setOf(name), inbox.unreadable)
        assertTrue(db.diagnostics().any { "unknown_version" in it })
    }

    @Test fun filenameAndBodyIdMustAgree() {
        folder.put("receipts", "$secondId.json", WeaverCodec.encode(receipt))
        assertTrue(CourierClient(folder, db).scan().receipts.isEmpty())
        assertTrue(db.diagnostics().single().contains("id_mismatch"))
    }

    @Test fun onlyOldPhoneTempsAreCleaned() {
        val old = ".$id.txt.0123456789abcdef.tmp"
        val fresh = ".$id.json.0123456789abcdef.tmp"
        folder.put("outbox", old, "partial")
        folder.put("outbox", fresh, "partial")
        folder.put("outbox", ".stfolder", "")
        folder.put("receipts", old, "server partial")
        folder.modified["receipts/$old"] = 1
        folder.modified["outbox/$old"] = 1
        folder.modified["outbox/$fresh"] = 700_000
        CourierClient(folder, db, now = { 700_001L }).scan()
        assertEquals(listOf("outbox/$old"), folder.deleted)
    }

    @Test fun securityFailureStopsScanInsteadOfContinuing() {
        folder.deny = true
        assertThrows(SecurityException::class.java) { CourierClient(folder, db).synchronize() }
    }

    @Test fun unsafePayloadAndVerdictCollisionAreRejected() {
        prepare(manifest.copy(payload = "elsewhere/body.txt"))
        assertFalse(CourierClient(folder, db).synchronize())
        val verdict = Verdict(2, "pkg_0123456789abcdef", 1, Verdict.Action.APPROVE, emptyList(), null, null)
        val client = CourierClient(folder, db)
        client.publishVerdict(verdict)
        client.publishVerdict(verdict)
        assertThrows(IllegalStateException::class.java) {
            client.publishVerdict(verdict.copy(action = Verdict.Action.REJECT))
        }
    }

    @Test fun invalidUtf8IsRejectedAndJsonReadIsBounded() {
        assertThrows(Exception::class.java) { CourierClient.boundedJson(byteArrayOf(0xc3.toByte()).inputStream()) }
        assertThrows(IllegalStateException::class.java) {
            CourierClient.boundedJson(ByteArray(CourierRules.maxJson + 1).inputStream())
        }
    }
}

internal class MemoryFolder : CourierFolder {
    val files = linkedMapOf<String, ByteArray>()
    val modified = mutableMapOf<String, Long>()
    val published = mutableListOf<String>()
    val deleted = mutableListOf<String>()
    var failName: String? = null
    var deny = false
    var root = "memory://courier"
    val createdFolders = mutableSetOf<String>()
    override val identity: String get() = root
    override fun created(folder: String): Boolean = folder in createdFolders
    fun put(folder: String, name: String, text: String) { files["$folder/$name"] = text.toByteArray() }
    override fun list(folder: String): List<CourierEntry> {
        if (deny) throw SecurityException("revoked")
        return files.keys.filter { it.startsWith("$folder/") }.map {
            val relative = it.removePrefix("$folder/")
            CourierEntry(relative.substringBefore('/'), modified[it] ?: 0, '/' !in relative)
        }.distinctBy { it.name }
    }
    override fun read(folder: String, name: String): InputStream = files.getValue("$folder/$name").inputStream()
    override fun publish(folder: String, name: String, write: (OutputStream) -> Unit) {
        check(!files.containsKey("$folder/$name"))
        if (name == failName) error("injected_failure")
        val stream = java.io.ByteArrayOutputStream()
        write(stream)
        files["$folder/$name"] = stream.toByteArray()
        published.add(name)
    }
    override fun delete(folder: String, name: String) {
        files.remove("$folder/$name"); deleted.add("$folder/$name")
    }
}
