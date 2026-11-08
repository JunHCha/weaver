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
            override fun markProcessed(callId: String, captureId: String) { processed.add(callId); pending = emptyList() }
        } }
    }
    @After fun restore() { PendingCalls.repository = originalRepository }
    private fun text(value: String = "공유한 테스트 텍스트") = SharedText.receive(context,
        Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, value))

    @Test fun ordinaryAndKakaoUseTextKindAndRemainDistinct() {
        listOf(Channel.SHARE_TEXT, Channel.KAKAO).forEach { channel ->
            val manifest = ShareSubmission.submit(context, text(channel.name), channel, null)
            assertEquals(CaptureKind.TEXT, manifest.kind)
            assertEquals(channel, manifest.channel)
            assertNull(manifest.occurredAt)
            assertNull(manifest.hints.counterpart)
        }
        CourierDatabase(context).use { assertEquals(2, it.captures().size) }
    }
    @Test fun replayAfterReopeningDatabaseReusesCapture() {
        val first = ShareSubmission.submit(context, text(), Channel.SHARE_TEXT, null)
        val repeated = ShareSubmission.submit(context, text(), Channel.SHARE_TEXT, null)
        assertEquals(first, repeated)
        CourierDatabase(context).use { assertEquals(1, it.captures().size) }
    }
    @Test fun callUsesMetadataAndIsProcessedOnlyAfterSuccessfulPublication() {
        val manifest = ShareSubmission.submit(context, text(), Channel.CALL, call.id)
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
    @Test fun queuedCallCannotReceiveSecondDifferentScript() {
        ShareSubmission.submit(context, text("first script"), Channel.CALL, call.id)
        assertThrows(IllegalArgumentException::class.java) {
            ShareSubmission.submit(context, text("another script"), Channel.CALL, call.id)
        }
        CourierDatabase(context).use { assertEquals(1, it.captures().size) }
    }
    @Test fun interruptedLinkWriteRecoversFromDurableDraft() {
        val manifest = ShareSubmission.submit(context, text(), Channel.CALL, call.id)
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
}
