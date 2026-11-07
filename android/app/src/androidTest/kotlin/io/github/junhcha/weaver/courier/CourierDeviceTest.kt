package io.github.junhcha.weaver.courier

import android.content.Context
import android.net.Uri
import android.provider.DocumentsContract
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import io.github.junhcha.weaver.schema.*
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/** Synthetic app-private data only; no permission or access to a user's actual Courier/vault. */
@RunWith(AndroidJUnit4::class)
class CourierDeviceTest {
    @Test fun sqliteRetainsCaptureAcrossReopenAndSuppressesDuplicateHash() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val bytes = "instrumentation synthetic capture".toByteArray()
        val id = "cap_01ARZ3NDEKTSV4RRFFQ69G5FAV"
        val stage = File(context.cacheDir, "instrumentation-courier.txt").apply { writeBytes(bytes) }
        val manifest = Manifest(1, id, CaptureKind.TEXT, Channel.SHARE_TEXT, "$id.txt",
            CourierClient.digest(bytes.inputStream()).first, bytes.size.toLong(),
            "2026-11-07T00:00:00Z", null, Manifest.Hints(null))
        val dbFile = File(context.filesDir, "courier.sqlite3")
        // Install-time test sandbox: preserve unrelated rows by using an isolated Context directory.
        val isolated = object : android.content.ContextWrapper(context) {
            override fun getFilesDir(): File = File(context.cacheDir, "courier-instrumentation").apply { mkdirs() }
        }
        try {
            CourierDatabase(isolated).use { db -> db.prepare("content://synthetic/one", manifest, stage) }
            CourierDatabase(isolated).use { db ->
                val other = manifest.copy(captureId = "cap_01ARZ3NDEKTSV4RRFFQ69G5FAW")
                assertEquals(id, db.prepare("content://synthetic/two", other, stage).manifest.captureId)
                assertEquals(1, db.captures().size)
            }
        } finally {
            stage.delete()
            File(isolated.filesDir, dbFile.name).delete()
        }
    }

    @Test fun jsonFailuresAreFailClosed() {
        assertTrue(WeaverCodec.decode<Receipt>("{") is DecodeResult.Invalid)
        assertTrue(WeaverCodec.decode<Receipt>("{\"schema_version\":2}") is DecodeResult.UnsupportedVersion)
        assertThrows(IllegalStateException::class.java) {
            CourierClient.boundedJson(ByteArray(CourierRules.maxJson + 1).inputStream())
        }
    }

    @Test fun ungrantedAndUntrustedTreesCannotRun() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val tree = DocumentsContract.buildTreeDocumentUri("com.android.externalstorage.documents",
            "primary:Documents/WeaverCourier")
        // A clean emulator install must have no grant. Never touch this tree if a tester granted it.
        org.junit.Assume.assumeFalse(context.contentResolver.persistedUriPermissions.any { it.uri == tree })
        assertThrows(IllegalStateException::class.java) {
            SafCourierFolder(context.contentResolver, tree).checkPermission()
        }
        assertThrows(IllegalArgumentException::class.java) {
            SafCourierFolder(context.contentResolver, Uri.parse("content://untrusted/tree/root"))
        }
    }
}
