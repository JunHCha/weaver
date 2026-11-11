package io.github.junhcha.weaver.pipeline

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.DocumentsContract as DC
import io.github.junhcha.weaver.schema.PipelinePage
import java.net.URLEncoder
import java.security.MessageDigest

/**
 * Read-only access to the phone's separately synchronized vault, never the server/user's vault.
 * Call select/arrived on an IO dispatcher. A filename or mtime alone does not prove arrival of the
 * server's latest page: the complete file must match the published SHA-256.
 */
object VaultAccess {
    private fun prefs(context: Context) =
        context.getSharedPreferences("pipeline-vault", Context.MODE_PRIVATE)

    fun name(context: Context): String = prefs(context).getString("name", "").orEmpty()

    fun tree(context: Context): Uri? = prefs(context).getString("tree", null)?.let(Uri::parse)

    fun setName(context: Context, value: String) {
        require(value.none { it.isISOControl() }) { "잘못된 vault 이름입니다" }
        check(prefs(context).edit().putString("name", value.trim()).commit()) { "설정을 저장하지 못했어요" }
    }

    fun select(context: Context, uri: Uri) {
        require(
            uri.scheme == "content" &&
                uri.authority == "com.android.externalstorage.documents" &&
                DC.isTreeUri(uri)
        ) {
            "폰에 동기화한 로컬 vault 폴더를 선택해 주세요"
        }
        val resolver = context.contentResolver
        resolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
        val root = DC.buildDocumentUriUsingTree(uri, DC.getTreeDocumentId(uri))
        resolver.query(root, arrayOf(DC.Document.COLUMN_MIME_TYPE), null, null)?.use {
            check(it.moveToFirst() && it.getString(0) == DC.Document.MIME_TYPE_DIR) {
                "vault 폴더를 읽을 수 없어요"
            }
        } ?: error("vault 폴더를 읽을 수 없어요")
        check(prefs(context).edit().putString("tree", uri.toString()).commit()) { "설정을 저장하지 못했어요" }
        // Do not release a previous tree grant here: Courier or another feature may share it.
    }

    fun arrived(context: Context, page: PipelinePage): Boolean {
        if (name(context).isBlank() || !safePath(page.path)) return false
        val selected = tree(context) ?: return false
        val resolver = context.contentResolver
        return try {
            if (resolver.persistedUriPermissions.none { it.uri == selected && it.isReadPermission })
                return false
            var parent = DC.buildDocumentUriUsingTree(selected, DC.getTreeDocumentId(selected))
            val parts = page.path.split('/')
            for ((index, part) in parts.withIndex()) {
                val query = DC.buildChildDocumentsUriUsingTree(selected, DC.getDocumentId(parent))
                val matches =
                    resolver
                        .query(
                            query,
                            arrayOf(
                                DC.Document.COLUMN_DOCUMENT_ID,
                                DC.Document.COLUMN_DISPLAY_NAME,
                                DC.Document.COLUMN_MIME_TYPE,
                            ),
                            null,
                            null,
                        )
                        ?.use { cursor ->
                            buildList<Pair<String, String>> {
                                while (cursor.moveToNext()) {
                                    if (cursor.getString(1) == part)
                                        add(cursor.getString(0) to cursor.getString(2))
                                }
                            }
                        } ?: return false
                val (id, mime) = matches.singleOrNull() ?: return false
                if (id != "${DC.getDocumentId(parent)}/$part") return false
                val isDirectory = mime == DC.Document.MIME_TYPE_DIR
                if (isDirectory != (index != parts.lastIndex)) return false
                parent = DC.buildDocumentUriUsingTree(selected, id)
            }
            val hash = MessageDigest.getInstance("SHA-256")
            val stream = resolver.openInputStream(parent) ?: return false
            stream.use { input ->
                val bytes = ByteArray(16 * 1024)
                var size = 0L
                while (true) {
                    val count = input.read(bytes)
                    if (count < 0) break
                    size += count
                    // Keep a corrupt/unbounded provider response from exhausting background IO.
                    if (size > 16L * 1024 * 1024) return false
                    hash.update(bytes, 0, count)
                }
            }
            hash.digest().joinToString("") { "%02x".format(it) } == page.sha256
        } catch (_: Exception) {
            false
        }
    }

    fun obsidianUri(vault: String, path: String): String {
        require(vault.isNotBlank() && safePath(path)) { "vault와 페이지 경로를 확인해 주세요" }
        fun encode(value: String) =
            URLEncoder.encode(value, Charsets.UTF_8.name()).replace("+", "%20")
        return "obsidian://open?vault=${encode(vault.trim())}&file=${encode(path)}"
    }

    private fun safePath(path: String): Boolean =
        path.isNotBlank() &&
            path.none { it == '\\' || it == ':' || it.isISOControl() } &&
            path.split('/').all { it.isNotBlank() && it != "." && it != ".." }
}
