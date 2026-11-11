package io.github.junhcha.weaver.pipeline

import android.content.Context
import android.content.Intent
import android.content.pm.ProviderInfo
import android.net.Uri
import android.provider.DocumentsContract as DC
import androidx.test.core.app.ApplicationProvider
import io.github.junhcha.weaver.courier.TestDocumentsProvider
import io.github.junhcha.weaver.schema.PipelinePage
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowContentResolver
import java.security.MessageDigest

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [26, 36])
class VaultAccessTest {
    private lateinit var context: Context
    private lateinit var provider: TestDocumentsProvider
    private val authority = "com.android.externalstorage.documents"
    private val tree = DC.buildTreeDocumentUri(authority, TestDocumentsProvider.ROOT)

    @Before fun setup() {
        context = ApplicationProvider.getApplicationContext()
        context.getSharedPreferences("pipeline-vault", Context.MODE_PRIVATE).edit().clear().commit()
        provider = TestDocumentsProvider()
        provider.attachInfo(context, ProviderInfo().apply {
            authority = this@VaultAccessTest.authority
            exported = true
            grantUriPermissions = true
            readPermission = "android.permission.MANAGE_DOCUMENTS"
            writePermission = "android.permission.MANAGE_DOCUMENTS"
        })
        ShadowContentResolver.registerProviderInternal(authority, provider)
    }

    private fun page(path: String, bytes: ByteArray): PipelinePage = PipelinePage(
        path = path,
        updatedAt = "2026-11-12T01:00:00Z",
        sha256 = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) },
    )

    private fun writePage(bytes: ByteArray) {
        val folder = provider.createDocument(TestDocumentsProvider.ROOT, DC.Document.MIME_TYPE_DIR, "wiki")
        val file = provider.createDocument(folder, "text/markdown", "예시 메모.md")
        provider.docs.getValue(file).file.writeBytes(bytes)
    }

    @Test fun selectionPersistsReadOnlyGrantAndExplicitVaultName() {
        assertEquals("", VaultAccess.name(context))
        assertNull(VaultAccess.tree(context))
        VaultAccess.select(context, tree)
        VaultAccess.setName(context, "  예시 Vault  ")
        assertEquals("예시 Vault", VaultAccess.name(context))
        assertEquals(tree, VaultAccess.tree(context))
        val permission = context.contentResolver.persistedUriPermissions.single { it.uri == tree }
        assertTrue(permission.isReadPermission)
        assertFalse(permission.isWritePermission)
    }

    @Test fun exactFileVersionMustArriveBeforeLinkIsEnabled() {
        val bytes = "# 가상 메모\n기록\n".toByteArray()
        val page = page("wiki/예시 메모.md", bytes)
        assertFalse(VaultAccess.arrived(context, page))
        VaultAccess.select(context, tree)
        VaultAccess.setName(context, "Weaver 예시")
        assertFalse(VaultAccess.arrived(context, page))
        writePage("older version".toByteArray())
        assertFalse(VaultAccess.arrived(context, page))
        provider.docs.getValue(TestDocumentsProvider.ROOT + "/wiki/예시 메모.md").file.writeBytes(bytes)
        assertTrue(VaultAccess.arrived(context, page))
        context.contentResolver.releasePersistableUriPermission(tree, Intent.FLAG_GRANT_READ_URI_PERMISSION)
        assertFalse(VaultAccess.arrived(context, page))
    }

    @Test fun renamedOrMissingVaultDoesNotGuessArrival() {
        val bytes = "sample".toByteArray()
        writePage(bytes)
        VaultAccess.select(context, tree)
        assertFalse(VaultAccess.arrived(context, page("wiki/예시 메모.md", bytes)))
        VaultAccess.setName(context, "Example")
        assertTrue(VaultAccess.arrived(context, page("wiki/예시 메모.md", bytes)))
        provider.docs.remove(TestDocumentsProvider.ROOT + "/wiki/예시 메모.md")
        assertFalse(VaultAccess.arrived(context, page("wiki/예시 메모.md", bytes)))
    }

    @Test fun duplicateNamesAndEscapingProviderIdsNeverEnableAResult() {
        val bytes = "sample".toByteArray()
        writePage(bytes)
        VaultAccess.select(context, tree)
        VaultAccess.setName(context, "Example")
        val id = TestDocumentsProvider.ROOT + "/wiki/예시 메모.md"
        val original = provider.docs.getValue(id)
        provider.docs["outside"] = original
        assertFalse(VaultAccess.arrived(context, page("wiki/예시 메모.md", bytes)))
        provider.docs.remove(id)
        // The returned display name alone is insufficient: this ID escapes its parent.
        assertFalse(VaultAccess.arrived(context, page("wiki/예시 메모.md", bytes)))
    }

    @Test fun obsidianLinksEncodeEachComponentAndRejectEscapingPaths() {
        assertEquals(
            "obsidian://open?vault=Weaver%20%EC%98%88%EC%8B%9C&file=wiki%2F%EC%98%88%EC%8B%9C%20%EB%A9%94%EB%AA%A8.md",
            VaultAccess.obsidianUri("Weaver 예시", "wiki/예시 메모.md"),
        )
        listOf("../outside.md", "/outside.md", "wiki/../outside.md", "wiki\\outside.md", "wiki//page.md").forEach { path ->
            assertThrows(IllegalArgumentException::class.java) { VaultAccess.obsidianUri("Example", path) }
        }
        assertThrows(IllegalArgumentException::class.java) { VaultAccess.select(context, Uri.parse("file:///tmp/vault")) }
    }
}
