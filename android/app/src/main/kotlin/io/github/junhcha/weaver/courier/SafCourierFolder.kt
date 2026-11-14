package io.github.junhcha.weaver.courier

import android.content.ContentResolver
import android.net.Uri
import android.provider.DocumentsContract as DC
import android.os.ParcelFileDescriptor
import java.io.InputStream
import java.io.OutputStream
import java.util.UUID

/** Only the primary external-storage provider is eligible for the device acceptance test. */
class SafCourierFolder(private val resolver: ContentResolver, val tree: Uri) : CourierFolder {
    init { require(allowed(tree)) { "Select primary Documents/WeaverCourier" } }
    private val createdDirectories = mutableSetOf<String>()
    override val identity: String get() = tree.toString()
    override fun created(folder: String): Boolean = folder in createdDirectories

    fun checkPermission() {
        check(resolver.persistedUriPermissions.any {
            it.uri == tree && it.isReadPermission && it.isWritePermission
        }) { "Courier permission lost; reselect folder" }
        children(root())
    }

    private fun root() = DC.buildDocumentUriUsingTree(tree, DC.getTreeDocumentId(tree))
    private data class Document(val uri: Uri, val name: String, val mime: String, val modified: Long, val safe: Boolean)
    private fun children(parent: Uri): List<Document> {
        val query = DC.buildChildDocumentsUriUsingTree(tree, DC.getDocumentId(parent))
        return resolver.query(query, arrayOf(DC.Document.COLUMN_DOCUMENT_ID,
            DC.Document.COLUMN_DISPLAY_NAME, DC.Document.COLUMN_MIME_TYPE,
            DC.Document.COLUMN_LAST_MODIFIED), null, null)?.use { cursor ->
            buildList {
                while (cursor.moveToNext()) {
                    val id = cursor.getString(0)
                    // ExternalStorage document IDs are path IDs; reject provider path escapes.
                    val parentId = DC.getDocumentId(parent)
                    val suffix = id.removePrefix("$parentId/")
                    val safe = id.startsWith("$parentId/") && CourierRules.safeName(suffix)
                    add(Document(if (safe) DC.buildDocumentUriUsingTree(tree, id) else Uri.EMPTY, cursor.getString(1),
                        cursor.getString(2), cursor.getLong(3), safe))
                }
            }
        } ?: error("Cannot enumerate Courier")
    }

    private fun directory(folder: String): Uri {
        require(folder in CourierRules.folders || folder in CourierRules.actionFolders || folder == "proposals/pipeline")
        val parent = if (folder == "proposals/pipeline") directory("proposals") else root()
        val leaf = folder.substringAfterLast('/')
        val found = children(parent).filter { it.name == leaf }
        check(found.size <= 1) { "duplicate_directory" }
        found.singleOrNull()?.let {
            check(it.safe && it.mime == DC.Document.MIME_TYPE_DIR) { "not_directory" }
            return it.uri
        }
        val created = DC.createDocument(resolver, parent, DC.Document.MIME_TYPE_DIR, leaf)
            ?: error("Cannot create Courier directory")
        try {
            check(displayName(created) == leaf) { "directory_name_changed" }
            createdDirectories.add(folder)
            return created
        } catch (error: Exception) {
            runCatching { DC.deleteDocument(resolver, created) }
            throw error
        }
    }

    private fun find(folder: String, name: String): Document? {
        require(CourierRules.safeName(name))
        val matches = children(directory(folder)).filter { it.name == name }
        check(matches.size <= 1) { "duplicate_name" }
        return matches.singleOrNull()?.also { check(it.safe) { "path_escape" } }
    }

    override fun list(folder: String): List<CourierEntry> = children(directory(folder)).map {
        CourierEntry(it.name, it.modified, it.safe && it.mime != DC.Document.MIME_TYPE_DIR)
    }

    override fun read(folder: String, name: String): InputStream {
        val doc = find(folder, name) ?: error("missing")
        check(doc.mime != DC.Document.MIME_TYPE_DIR) { "not_regular_file" }
        return resolver.openInputStream(doc.uri) ?: error("unreadable")
    }

    override fun publish(folder: String, name: String, write: (OutputStream) -> Unit) {
        require(CourierRules.safeName(name) && !CourierRules.ignored(name))
        check(find(folder, name) == null) { "already_exists" }
        val token = UUID.randomUUID().toString().replace("-", "").take(16)
        val tempName = ".$name.$token.tmp"
        var owned = DC.createDocument(resolver, directory(folder), "application/octet-stream", tempName)
            ?: error("create_failed")
        try {
            check(displayName(owned) == tempName) { "temporary_name_changed" }
            val descriptor = resolver.openFileDescriptor(owned, "w") ?: error("open_failed")
            ParcelFileDescriptor.AutoCloseOutputStream(descriptor).use { output ->
                write(output)
                output.flush()
                descriptor.fileDescriptor.sync()
            }
            check(find(folder, name) == null) { "already_exists" }
            owned = DC.renameDocument(resolver, owned, name) ?: error("rename_failed")
            check(displayName(owned) == name) { "final_name_changed" }
        } catch (error: Exception) {
            runCatching { DC.deleteDocument(resolver, owned) }
            throw error
        }
    }

    private fun displayName(uri: Uri): String = resolver.query(uri,
        arrayOf(DC.Document.COLUMN_DISPLAY_NAME), null, null)?.use {
        check(it.moveToFirst()) { "missing_document" }
        it.getString(0)
    } ?: error("query_failed")

    override fun delete(folder: String, name: String) {
        find(folder, name)?.let { check(DC.deleteDocument(resolver, it.uri)) { "delete_failed" } }
    }

    /** Functional probe only; concurrent-reader atomicity is a separate diagnostic. */
    fun selfTest(): List<String> = CourierRules.folders.map { folder ->
        val warnings = if (list(folder).none { it.name == ".stfolder" }) " (.stfolder missing)" else ""
        val name = "probe-${UUID.randomUUID()}.txt"
        try {
            publish(folder, name) { it.write("WeaverCourier probe".toByteArray()) }
            check(read(folder, name).use { it.readBytes().decodeToString() } == "WeaverCourier probe")
            "$folder: write/rename/read/delete OK$warnings"
        } finally { delete(folder, name) }
    }

    companion object {
        fun allowed(uri: Uri): Boolean = runCatching {
            uri.scheme == "content" && uri.authority == "com.android.externalstorage.documents" &&
                DC.isTreeUri(uri) && DC.getTreeDocumentId(uri) == "primary:Documents/WeaverCourier"
        }.getOrDefault(false)
    }
}
