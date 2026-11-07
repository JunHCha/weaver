package io.github.junhcha.weaver.courier

import android.content.Context
import android.content.Intent
import android.content.pm.ProviderInfo
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.os.CancellationSignal
import android.os.ParcelFileDescriptor
import android.provider.DocumentsContract as DC
import android.provider.DocumentsProvider
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowContentResolver
import java.io.File
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [26, 36])
class SafCourierFolderTest {
    private lateinit var context: Context
    private lateinit var provider: TestDocumentsProvider
    private lateinit var folder: SafCourierFolder
    private val tree = DC.buildTreeDocumentUri("com.android.externalstorage.documents", TestDocumentsProvider.ROOT)

    @Before fun setup() {
        context = ApplicationProvider.getApplicationContext()
        provider = TestDocumentsProvider()
        provider.attachInfo(context, ProviderInfo().apply {
            authority = "com.android.externalstorage.documents"; exported = true; grantUriPermissions = true
            readPermission = "android.permission.MANAGE_DOCUMENTS"
            writePermission = "android.permission.MANAGE_DOCUMENTS"
        })
        ShadowContentResolver.registerProviderInternal("com.android.externalstorage.documents", provider)
        folder = SafCourierFolder(context.contentResolver, tree)
    }

    @Test fun publishesViaTemporaryAndReturnedRenameUriWithoutOverwrite() {
        folder.publish("outbox", "capture.txt") { it.write("complete".toByteArray()) }
        assertEquals("complete", folder.read("outbox", "capture.txt").use { it.readBytes().decodeToString() })
        assertTrue(provider.events.first().startsWith("create:.capture.txt."))
        assertEquals("rename:capture.txt", provider.events.last())
        assertThrows(IllegalStateException::class.java) { folder.publish("outbox", "capture.txt") {} }
        folder.delete("outbox", "capture.txt")
        assertTrue(folder.list("outbox").isEmpty())
    }

    @Test fun renamedSuffixFailsAndCleansReturnedDocument() {
        provider.changeFinalName = true
        assertThrows(IllegalStateException::class.java) { folder.publish("outbox", "capture.txt") {} }
        assertTrue(folder.list("outbox").isEmpty())
    }

    @Test fun writeFailureCleansTemporaryAndNeverRenames() {
        assertThrows(IllegalStateException::class.java) {
            folder.publish("outbox", "capture.txt") { it.write(1); error("injected") }
        }
        assertTrue(folder.list("outbox").isEmpty())
        assertFalse(provider.events.any { it.startsWith("rename") })
    }

    @Test fun selfTestCoversAllDirectoriesAndPreservesSyncthingMarker() {
        val reports = folder.selfTest()
        assertEquals(4, reports.size)
        reports.forEach { assertTrue(it.contains(".stfolder missing")) }
        CourierRules.folders.forEach { assertTrue(folder.list(it).isEmpty()) }
    }

    @Test fun untrustedProviderAndTreeAreRejected() {
        listOf("content://cloud/tree/primary%3ADocuments%2FWeaverCourier",
            "content://com.android.externalstorage.documents/tree/primary%3A",
            "content://com.android.externalstorage.documents/tree/primary%3AAndroid%2Fdata").forEach {
            assertFalse(SafCourierFolder.allowed(Uri.parse(it)))
        }
        assertTrue(SafCourierFolder.allowed(tree))
    }

    @Test fun permissionRevocationStopsAndPersistedRegrantRecovers() {
        assertThrows(IllegalStateException::class.java) { folder.checkPermission() }
        val flags = Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION
        context.contentResolver.takePersistableUriPermission(tree, flags)
        folder.checkPermission()
        context.contentResolver.releasePersistableUriPermission(tree, flags)
        assertThrows(IllegalStateException::class.java) { folder.checkPermission() }
        context.contentResolver.takePersistableUriPermission(tree, flags)
        folder.checkPermission()
    }

    @Test fun pathTraversalIsRejected() {
        assertThrows(IllegalArgumentException::class.java) { folder.read("outbox", "../outside") }
        assertThrows(IllegalArgumentException::class.java) { folder.list("../outbox") }
    }
}

/** Synthetic provider only. Exercises framework DocumentsContract without shared storage or user data. */
class TestDocumentsProvider : DocumentsProvider() {
    data class Doc(val parent: String?, val name: String, val mime: String, val file: File)
    val docs = linkedMapOf<String, Doc>()
    val events = mutableListOf<String>()
    var changeFinalName = false
    companion object { const val ROOT = "primary:Documents/WeaverCourier" }
    override fun onCreate(): Boolean {
        docs[ROOT] = Doc(null, "WeaverCourier", DC.Document.MIME_TYPE_DIR, context!!.cacheDir)
        return true
    }
    override fun isChildDocument(parentDocumentId: String, documentId: String): Boolean =
        documentId.startsWith("$parentDocumentId/")
    override fun queryRoots(projection: Array<out String>?): Cursor = MatrixCursor(arrayOf(DC.Root.COLUMN_ROOT_ID))
    private fun cursor(values: List<Pair<String, Doc>>, projection: Array<out String>?): Cursor {
        val columns = projection ?: arrayOf(DC.Document.COLUMN_DOCUMENT_ID, DC.Document.COLUMN_DISPLAY_NAME,
            DC.Document.COLUMN_MIME_TYPE, DC.Document.COLUMN_LAST_MODIFIED)
        return MatrixCursor(columns).apply { values.forEach { (id, doc) ->
            addRow(columns.map { column -> when (column) {
                DC.Document.COLUMN_DOCUMENT_ID -> id
                DC.Document.COLUMN_DISPLAY_NAME -> doc.name
                DC.Document.COLUMN_MIME_TYPE -> doc.mime
                DC.Document.COLUMN_LAST_MODIFIED -> doc.file.lastModified()
                else -> null
            } }.toTypedArray<Any?>())
        } }
    }
    override fun queryDocument(documentId: String, projection: Array<out String>?): Cursor =
        cursor(listOf(documentId to docs.getValue(documentId)), projection)
    override fun queryChildDocuments(parentDocumentId: String, projection: Array<out String>?, sortOrder: String?): Cursor =
        cursor(docs.entries.filter { it.value.parent == parentDocumentId }.map { it.key to it.value }, projection)
    override fun openDocument(documentId: String, mode: String, signal: CancellationSignal?): ParcelFileDescriptor =
        ParcelFileDescriptor.open(docs.getValue(documentId).file, ParcelFileDescriptor.parseMode(mode))
    override fun createDocument(parentDocumentId: String, mimeType: String, displayName: String): String {
        val id = "$parentDocumentId/$displayName"
        val file = File(context!!.cacheDir, UUID.randomUUID().toString()).apply { createNewFile() }
        docs[id] = Doc(parentDocumentId, displayName, mimeType, file)
        if (mimeType != DC.Document.MIME_TYPE_DIR) events.add("create:$displayName")
        return id
    }
    override fun renameDocument(documentId: String, displayName: String): String {
        val doc = docs.remove(documentId)!!
        val final = if (changeFinalName) "$displayName (1)" else displayName
        val id = "${doc.parent}/$final"
        docs[id] = doc.copy(name = final)
        events.add("rename:$final")
        return id
    }
    override fun deleteDocument(documentId: String) { docs.remove(documentId)!!.file.delete() }
}
