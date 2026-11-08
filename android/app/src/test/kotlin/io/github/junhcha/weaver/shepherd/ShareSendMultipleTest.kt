package io.github.junhcha.weaver.shepherd

import android.content.ClipData
import android.content.ContentProvider
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.provider.OpenableColumns
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.io.ByteArrayInputStream
import java.io.File

/** Samsung 음성 녹음 앱의 SEND_MULTIPLE(application/txt) 통화 스크립트 공유. 합성 데이터만 사용한다. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [26, 36])
class ShareSendMultipleTest {
    private lateinit var context: Context

    class NameProvider : ContentProvider() {
        override fun onCreate() = true
        override fun query(uri: Uri, projection: Array<out String>?, selection: String?, args: Array<out String>?, sort: String?): Cursor =
            MatrixCursor(arrayOf(OpenableColumns.DISPLAY_NAME)).apply { addRow(arrayOf(names[uri.lastPathSegment])) }
        override fun getType(uri: Uri): String? = types[uri.lastPathSegment]
        override fun insert(uri: Uri, values: ContentValues?): Uri? = null
        override fun delete(uri: Uri, selection: String?, args: Array<out String>?) = 0
        override fun update(uri: Uri, values: ContentValues?, selection: String?, args: Array<out String>?) = 0
        companion object {
            val names = mutableMapOf<String, String?>()
            val types = mutableMapOf<String, String>()
        }
    }

    @Before fun setup() {
        context = ApplicationProvider.getApplicationContext()
        File(context.filesDir, "shepherd-shares").deleteRecursively()
        NameProvider.names.clear()
        NameProvider.types.clear()
        Robolectric.buildContentProvider(NameProvider::class.java).create("test.share")
    }

    private fun shared(
        name: String,
        content: String = "합성 통화 스크립트\n나  (00:01)\n안녕하세요\n",
        displayName: String? = name,
        type: String? = null,
    ) = shared(name, content.toByteArray(), displayName, type)
    private fun shared(name: String, bytes: ByteArray, displayName: String? = name, type: String? = null): Uri {
        val uri = Uri.parse("content://test.share/$name")
        NameProvider.names[name] = displayName
        type?.let { NameProvider.types[name] = it }
        shadowOf(context.contentResolver).registerInputStream(uri, ByteArrayInputStream(bytes))
        return uri
    }
    private fun multiple(type: String, vararg uris: Uri) = Intent(Intent.ACTION_SEND_MULTIPLE).setType(type)
        .putParcelableArrayListExtra(Intent.EXTRA_STREAM, ArrayList(uris.toList()))
    private fun saved() = File(context.filesDir, "shepherd-shares").listFiles().orEmpty().size

    @Test fun savesFileContentAndIgnoresAccompanyingExtraText() {
        val uri = shared("call.TXT")
        val intent = multiple("application/txt", uri).putExtra(Intent.EXTRA_TEXT, "미리보기 본문")
        val file = SharedText.receive(context, intent)
        assertEquals("합성 통화 스크립트\n나  (00:01)\n안녕하세요\n", file.readText())
        assertEquals("call.TXT", SharedText.displayName(context, intent))
    }
    @Test fun singleSendWithStreamAndTextUsesFile() {
        val intent = Intent(Intent.ACTION_SEND).setType("application/txt")
            .putExtra(Intent.EXTRA_STREAM, shared("a.txt")).putExtra(Intent.EXTRA_TEXT, "무시")
        assertTrue(SharedText.receive(context, intent).readText().startsWith("합성 통화"))
    }
    @Test fun rejectsZeroOrTwoStreamsWithoutSaving() {
        val none = Intent(Intent.ACTION_SEND_MULTIPLE).setType("application/txt")
        val two = multiple("application/txt", shared("a.txt"), shared("b.txt"))
        listOf(none, multiple("application/txt"), two).forEach {
            val error = assertThrows(IllegalArgumentException::class.java) { SharedText.receive(context, it) }
            assertTrue(error.message!!.contains("한 개"))
        }
        assertEquals(0, saved())
    }
    private fun rejection(intent: Intent): String =
        assertThrows(IllegalArgumentException::class.java) { SharedText.receive(context, intent) }.message!!

    @Test fun rejectsNonTxtDisplayName() {
        val message = rejection(multiple("application/txt", shared("voice.m4a")))
        assertTrue(message, message.contains("확장자가 .txt"))
        assertEquals(0, saved())
    }
    @Test fun rejectsNullDisplayName() {
        val message = rejection(multiple("application/txt", shared("noname", displayName = null)))
        assertTrue(message, message.contains("확장자가 .txt"))
        assertEquals(0, saved())
    }
    @Test fun rejectsNonTxtNameWhenOnlyResolvedTypeIsApplicationTxt() {
        val uri = shared("voice.m4a", type = "application/txt")
        val message = rejection(multiple("text/plain", uri))
        assertTrue(message, message.contains("확장자가 .txt"))
        assertEquals(0, saved())
    }
    @Test fun rejectsAudioAndMixedTypes() {
        listOf("audio/mpeg", "audio/*", "application/txt, audio/*", "*/*", "application/octet-stream").forEach {
            val message = rejection(multiple(it, shared("a.txt")))
            assertTrue("$it: $message", message.contains("음성·오디오"))
        }
        assertEquals(0, saved())
    }
    @Test fun rejectsAudioResolvedTypeFromProvider() {
        val message = rejection(multiple("application/txt", shared("a.txt", type = "audio/mpeg")))
        assertTrue(message, message.contains("text/plain 텍스트 파일만"))
        assertEquals(0, saved())
    }
    @Test fun rejectsTwoClipDataItems() {
        val clip = ClipData.newRawUri("s", shared("a.txt")).apply { addItem(ClipData.Item(shared("b.txt"))) }
        val intent = Intent(Intent.ACTION_SEND).setType("application/txt")
            .putExtra(Intent.EXTRA_STREAM, shared("c.txt")).apply { clipData = clip }
        val message = rejection(intent)
        assertTrue(message, message.contains("한 번에 한 항목"))
        assertEquals(0, saved())
    }
    @Test fun removesPartialFileWhenSendMultipleExceedsLimit() {
        val big = ByteArray((SharedText.MAX_BYTES + 1).toInt()) { 'a'.code.toByte() }
        val message = rejection(multiple("application/txt", shared("big.txt", big)))
        assertTrue(message, message.contains("10 MiB"))
        assertEquals(0, saved())
    }
    @Test fun removesFileWhenSendMultipleContainsNul() {
        val message = rejection(multiple("application/txt", shared("nul.txt", "a\u0000b")))
        assertTrue(message, message.contains("NUL"))
        assertEquals(0, saved())
    }
    @Test fun manifestAcceptsOnlyTextTypesForSendAndSendMultiple() {
        val pm = context.packageManager
        fun accepts(action: String, type: String) = pm.queryIntentActivities(
            Intent(action).setType(type).addCategory(Intent.CATEGORY_DEFAULT).setPackage(context.packageName), 0)
            .any { it.activityInfo.name.endsWith("ShareActivity") }
        listOf(Intent.ACTION_SEND, Intent.ACTION_SEND_MULTIPLE).forEach { action ->
            assertTrue(accepts(action, "text/plain"))
            assertTrue(accepts(action, "application/txt"))
            listOf("audio/mpeg", "audio/mp4", "application/octet-stream", "application/pdf").forEach {
                assertFalse("$action $it", accepts(action, it))
            }
        }
        // Gradle runs unit tests from the module dir; also accept the repo root so this does not depend on it.
        val manifest = listOf("src/main/AndroidManifest.xml", "app/src/main/AndroidManifest.xml", "android/app/src/main/AndroidManifest.xml")
            .map(::File).first { it.isFile }
        val xml = manifest.readText()
            .substringAfter(".shepherd.ShareActivity").substringBefore("</activity>")
        assertFalse(xml.contains("audio"))
        assertFalse(xml.contains("mimeType=\"*/*\"") || xml.contains("application/*"))
    }
}
