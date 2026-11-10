package io.github.junhcha.weaver.shepherd

import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.test.core.app.ApplicationProvider
import io.github.junhcha.weaver.courier.CourierDatabase
import io.github.junhcha.weaver.schema.CaptureKind
import io.github.junhcha.weaver.schema.Channel
import org.junit.Assert.*
import org.junit.Before
import org.junit.After
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.Shadows.shadowOf
import java.io.ByteArrayInputStream
import java.io.File

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [26, 36])
class ShareSubmissionTest {
    private lateinit var context: Context
    private val originalRepository = PendingCalls.repository
    private val processed = mutableListOf<String>()
    private val call = PendingCall("call-1", "2026-11-08T12:00:00+09:00", "테스트 상대")
    private var pending = listOf(call)

    @Before fun setup() {
        context = ApplicationProvider.getApplicationContext()
        context.deleteDatabase(File(context.filesDir, "courier.sqlite3").absolutePath)
        listOf("courier", "shepherd-call-links", "shepherd-call-drafts", "shepherd-call-assignments").forEach {
            context.getSharedPreferences(it, Context.MODE_PRIVATE).edit().clear().commit()
        }
        File(context.filesDir, "courier-staging").deleteRecursively()
        File(context.filesDir, "shepherd-shares").deleteRecursively()
        PendingCalls.repository = { object : PendingCallRepository {
            override fun pending() = pending
            override fun markProcessed(callId: String, captureId: String): Boolean { processed.add(callId); pending = emptyList(); return true }
        } }
    }
    @After fun restore() { PendingCalls.repository = originalRepository }
    private fun text(value: String = "공유한 테스트 텍스트") = SharedText.receive(context,
        Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, value))

    @Test fun utf16TranscriptsQueueBomlessUtf8WithNormalizedSizeAndDigest() {
        val content = "AI로 생성된 콘텐츠입니다\n\n가짜 대화 😀 \n"
        listOf(Charsets.UTF_16BE to byteArrayOf(0xfe.toByte(), 0xff.toByte()),
            Charsets.UTF_16LE to byteArrayOf(0xff.toByte(), 0xfe.toByte())).forEachIndexed { index, (charset, bom) ->
            val uri = Uri.parse("content://test/utf16-$index")
            shadowOf(context.contentResolver).registerInputStream(uri, ByteArrayInputStream(bom + content.toByteArray(charset)))
            val file = SharedText.receive(context, Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_STREAM, uri))
            val expectedDigest = SharedText.digest(file)
            val result = ShareSubmission.submit(context, file, Channel.CALL, call.id)
            assertEquals(index == 1, result.duplicate)
            assertEquals(content.toByteArray().size.toLong(), result.manifest.size)
            assertEquals(expectedDigest, result.manifest.sha256)
            CourierDatabase(context).use { db ->
                assertEquals(1, db.captures().size)
                assertArrayEquals(content.toByteArray(), File(db.captures().single().stagedPath).readBytes())
            }
        }
    }
    @Test fun ordinaryAndKakaoUseTextKindAndRemainDistinct() {
        listOf(Channel.SHARE_TEXT, Channel.KAKAO).forEach { channel ->
            val manifest = ShareSubmission.submit(context, text(channel.name), channel, null).manifest
            assertEquals(CaptureKind.TEXT, manifest.kind)
            assertEquals(channel, manifest.channel)
            assertNull(manifest.occurredAt)
            assertNull(manifest.hints.counterpart)
        }
        CourierDatabase(context).use { assertEquals(2, it.captures().size) }
    }
    @Test fun replayAfterReopeningDatabaseReusesCapture() {
        val first = ShareSubmission.submit(context, text(), Channel.SHARE_TEXT, null).manifest
        val repeated = ShareSubmission.submit(context, text(), Channel.SHARE_TEXT, null).manifest
        assertEquals(first, repeated)
        CourierDatabase(context).use { assertEquals(1, it.captures().size) }
    }
    @Test fun callUsesMetadataAndIsProcessedOnlyAfterSuccessfulPublication() {
        val manifest = ShareSubmission.submit(context, text(), Channel.CALL, call.id).manifest
        assertEquals(call.occurredAt, manifest.occurredAt)
        assertEquals(call.counterpart, manifest.hints.counterpart)
        PendingCalls.reconcile(context)
        assertTrue(processed.isEmpty())
        CourierDatabase(context).use { it.state(manifest.captureId, "SENT", 1) }
        PendingCalls.reconcile(context)
        PendingCalls.reconcile(context)
        assertEquals(listOf(call.id), processed)
        assertThrows(IllegalArgumentException::class.java) {
            ShareSubmission.submit(context, text(), Channel.CALL, "next-call")
        }
        CourierDatabase(context).use { assertEquals(1, it.captures().size) }
    }
    @Test fun queuedCallCannotReceiveSecondDifferentThread() {
        ShareSubmission.submit(context, text("first script"), Channel.CALL, call.id)
        assertThrows(IllegalArgumentException::class.java) {
            ShareSubmission.submit(context, text("another script"), Channel.CALL, call.id)
        }
        CourierDatabase(context).use { assertEquals(1, it.captures().size) }
    }
    @Test fun interruptedLinkWriteRecoversFromDurableDraft() {
        val manifest = ShareSubmission.submit(context, text(), Channel.CALL, call.id).manifest
        context.getSharedPreferences("shepherd-call-links", Context.MODE_PRIVATE).edit().clear().commit()
        context.getSharedPreferences("shepherd-call-drafts", Context.MODE_PRIVATE).edit()
            .putString(manifest.sha256, call.id).commit()
        CourierDatabase(context).use { it.state(manifest.captureId, "SENT", 1) }
        PendingCalls.reconcile(context)
        assertEquals(listOf(call.id), processed)
    }
    @Test fun revokedPermissionWhileReadingCleansSnapshot() {
        val uri = Uri.parse("content://test/revoked-during-read")
        shadowOf(context.contentResolver).registerInputStream(uri, object : java.io.InputStream() {
            override fun read(): Int = throw SecurityException("revoked")
        })
        assertThrows(SecurityException::class.java) {
            SharedText.receive(context, Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_STREAM, uri))
        }
        assertTrue(File(context.filesDir, "shepherd-shares").listFiles().isNullOrEmpty())
        CourierDatabase(context).use { assertTrue(it.captures().isEmpty()) }
    }
    @Test fun noPendingCallCannotCreateManifest() {
        pending = emptyList()
        assertThrows(IllegalStateException::class.java) {
            ShareSubmission.submit(context, text(), Channel.CALL, null)
        }
        CourierDatabase(context).use { assertTrue(it.captures().isEmpty()) }
    }
    @Test fun rejectsUnsupportedMultipleEmptyAndUnreadableBeforeCourier() {
        val invalid = listOf(
            Intent(Intent.ACTION_SEND_MULTIPLE).setType("text/plain"),
            Intent(Intent.ACTION_SEND).setType("audio/mpeg"),
            Intent(Intent.ACTION_SEND).setType("image/png"),
            Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, ""),
            Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_STREAM, Uri.parse("file:///tmp/no")),
            Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_STREAM, Uri.parse("content://test/revoked")),
        )
        invalid.forEach { intent -> assertThrows(Exception::class.java) { SharedText.receive(context, intent) } }
        CourierDatabase(context).use { assertTrue(it.captures().isEmpty()) }
        assertTrue(File(context.filesDir, "shepherd-shares").listFiles().isNullOrEmpty())
    }
    @Test fun streamsContentUriAndIgnoresSenderDisplayName() {
        val uri = Uri.parse("content://test/../../evil.json")
        val bytes = "\uFEFF본문".toByteArray()
        shadowOf(context.contentResolver).registerInputStream(uri, ByteArrayInputStream(bytes))
        val file = SharedText.receive(context, Intent(Intent.ACTION_SEND).setType("text/plain")
            .putExtra(Intent.EXTRA_STREAM, uri))
        assertArrayEquals(bytes, file.readBytes())
        assertEquals(File(context.filesDir, "shepherd-shares"), file.parentFile)
        assertFalse(file.name.contains("evil"))
    }
    @Test fun falseAndThrowingCallsKeepLinksAndOtherCallsContinue() {
        val call2 = call.copy(id = "call-2")
        pending = listOf(call, call2)
        val first = ShareSubmission.submit(context, text("first"), Channel.CALL, call.id).manifest
        val second = ShareSubmission.submit(context, text("second"), Channel.CALL, call2.id).manifest
        CourierDatabase(context).use { db ->
            db.state(first.captureId, "SENT", 1); db.state(second.captureId, "SENT", 1)
        }
        var throwFirst = false
        PendingCalls.repository = { object : PendingCallRepository {
            override fun pending() = pending
            override fun markProcessed(callId: String, captureId: String): Boolean {
                if (callId == call.id) {
                    if (throwFirst) error("synthetic_failure")
                    return false
                }
                processed.add(callId); return true
            }
        } }
        assertFalse(PendingCalls.reconcile(context))
        val links = context.getSharedPreferences("shepherd-call-links", Context.MODE_PRIVATE)
        assertEquals(setOf(first.captureId), links.all.keys)
        assertEquals(listOf(call2.id), processed)
        throwFirst = true
        processed.clear()
        links.edit().putString(second.captureId, call2.id).commit()
        assertFalse(PendingCalls.reconcile(context))
        assertEquals(listOf(call2.id), processed)
        assertTrue(links.contains(first.captureId))
        CourierDatabase(context).use { assertTrue(it.diagnostics().any { line -> line.contains("shepherd/reconcile") }) }
        PendingCalls.repository = { object : PendingCallRepository {
            override fun pending() = pending
            override fun markProcessed(callId: String, captureId: String) = true
        } }
        assertTrue(PendingCalls.reconcile(context))
        assertTrue(links.all.isEmpty())
    }
    @Test fun adapterFiltersProcessedSortsInstantsAndKeepsUnknownLast() {
        val records = listOf(
            CallRecord("unknown", "unknown file", null, null, CallStatus.UNSHARED),
            CallRecord("older", "old file", null, "2026-11-08T13:00:00+09:00", CallStatus.UNSHARED),
            CallRecord("processed", "done", null, "2026-11-09T01:00:00Z", CallStatus.PROCESSED),
            CallRecord("recent", "new file", null, "2026-11-08T05:00:00Z", CallStatus.UNSHARED))
        val result = PendingCalls.adapt(records)
        assertEquals(listOf("recent", "older", "unknown"), result.map { it.id })
        assertEquals("new file", result.first().displayName)
    }
    @Test fun invalidMetadataNormalizesBeforePersistingAndPrepareCleansFailedManifest() {
        pending = listOf(call.copy(occurredAt = "2026-11-08T12:00+09:00", counterpart = "  "))
        val manifest = ShareSubmission.submit(context, text(), Channel.CALL, call.id).manifest
        assertNull(manifest.occurredAt); assertNull(manifest.hints.counterpart)
        val before = File(context.filesDir, "courier-staging").listFiles()!!.map { it.name }.toSet()
        val source = File(context.cacheDir, "invalid-meta.txt").apply { writeText("different") }
        assertThrows(IllegalArgumentException::class.java) {
            io.github.junhcha.weaver.courier.CourierRuntime.prepare(context, Uri.fromFile(source),
                CaptureKind.TEXT, Channel.CALL, "txt", "invalid", "  ")
        }
        assertEquals(before, File(context.filesDir, "courier-staging").listFiles()!!.map { it.name }.toSet())
    }
    @Test fun crashDraftReservesCallBeforeNextSubmission() {
        val first = ShareSubmission.submit(context, text("first"), Channel.CALL, call.id).manifest
        context.getSharedPreferences("shepherd-call-links", Context.MODE_PRIVATE).edit().clear().commit()
        context.getSharedPreferences("shepherd-call-drafts", Context.MODE_PRIVATE).edit()
            .putString(first.sha256, call.id).commit()
        assertThrows(IllegalArgumentException::class.java) {
            ShareSubmission.submit(context, text("second"), Channel.CALL, call.id)
        }
        CourierDatabase(context).use { assertEquals(1, it.captures().size) }
    }
    @Test fun duplicateIsExplicitAndPrivateUrisAreRejected() {
        assertFalse(ShareSubmission.submit(context, text(), Channel.SHARE_TEXT, null).duplicate)
        assertTrue(ShareSubmission.submit(context, text(), Channel.SHARE_TEXT, null).duplicate)
        val root = "content://com.android.externalstorage.documents/tree/primary%3ADocuments%2FWeaverCourier"
        context.getSharedPreferences("courier", Context.MODE_PRIVATE).edit().putString("tree", root).commit()
        listOf("content://${context.packageName}.files/private.txt",
            "$root/document/primary%3ADocuments%2FWeaverCourier%2Foutbox%2Fcap.txt",
            "content://com.android.externalstorage.documents/document/primary%3ADocuments%2FWeaverCourier%2Foutbox%2Fcap.txt"
        ).forEach { uri ->
            assertThrows(IllegalArgumentException::class.java) {
                SharedText.receive(context, Intent(Intent.ACTION_SEND).setType("text/plain")
                    .putExtra(Intent.EXTRA_STREAM, Uri.parse(uri)))
            }
        }
    }

}
