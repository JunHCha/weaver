package io.github.junhcha.weaver.shepherd

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
        override fun getType(uri: Uri): String? = null
        override fun insert(uri: Uri, values: ContentValues?): Uri? = null
        override fun delete(uri: Uri, selection: String?, args: Array<out String>?) = 0
        override fun update(uri: Uri, values: ContentValues?, selection: String?, args: Array<out String>?) = 0
        companion object { val names = mutableMapOf<String, String>() }
    }

    @Before fun setup() {
        context = ApplicationProvider.getApplicationContext()
        File(context.filesDir, "shepherd-shares").deleteRecursively()
        NameProvider.names.clear()
        Robolectric.buildContentProvider(NameProvider::class.java).create("test.share")
    }

    private fun shared(name: String, content: String = "합성 통화 스크립트\n나  (00:01)\n안녕하세요\n"): Uri {
        val uri = Uri.parse("content://test.share/$name")
        NameProvider.names[name] = name
        shadowOf(context.contentResolver).registerInputStream(uri, ByteArrayInputStream(content.toByteArray()))
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
    @Test fun rejectsNonTxtDisplayName() {
        val intent = multiple("application/txt", shared("voice.m4a"))
        assertThrows(IllegalArgumentException::class.java) { SharedText.receive(context, intent) }
        assertEquals(0, saved())
    }
    @Test fun rejectsAudioAndMixedTypes() {
        listOf("audio/mpeg", "audio/*", "application/txt, audio/*", "*/*", "application/octet-stream").forEach {
            assertThrows(it, IllegalArgumentException::class.java) {
                SharedText.receive(context, multiple(it, shared("a.txt")))
            }
        }
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
        val xml = File("src/main/AndroidManifest.xml").readText()
            .substringAfter(".shepherd.ShareActivity").substringBefore("</activity>")
        assertFalse(xml.contains("audio"))
        assertFalse(xml.contains("mimeType=\"*/*\"") || xml.contains("application/*"))
    }
}
