package io.github.junhcha.weaver.courier

import android.content.Context
import android.content.Intent
import android.content.pm.ProviderInfo
import android.net.Uri
import android.provider.DocumentsContract as DC
import androidx.test.core.app.ApplicationProvider
import androidx.work.Configuration
import androidx.work.ListenableWorker
import androidx.work.WorkerFactory
import androidx.work.WorkerParameters
import androidx.work.Worker
import androidx.work.testing.SynchronousExecutor
import androidx.work.testing.WorkManagerTestInitHelper
import io.github.junhcha.weaver.pipeline.VaultAccess
import io.github.junhcha.weaver.schema.PipelinePage
import org.junit.Assert.*
import org.junit.After
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowContentResolver
import java.security.MessageDigest

/**
 * Courier's own folder grant and the phone vault grant (W5-A6 / I6) are independent: choosing, replacing
 * or failing to choose a Courier folder must never release a grant the vault still relies on.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [26, 36])
class CourierGrantIsolationTest {
    private lateinit var context: Context
    private lateinit var provider: TestDocumentsProvider
    private val authority = "com.android.externalstorage.documents"
    private val rw = Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION
    private val courierTree = DC.buildTreeDocumentUri(authority, TestDocumentsProvider.ROOT)

    /** Same Courier folder spelled as a document under the tree: a different URI value for the same place. */
    private val courierAlias = DC.buildDocumentUriUsingTree(courierTree, TestDocumentsProvider.ROOT)

    /** A vault that is not the Courier folder at all. */
    private val vaultTree = DC.buildTreeDocumentUri(authority, "primary:Vault")

    @Before fun setup() {
        context = ApplicationProvider.getApplicationContext()
        listOf("courier", "pipeline-vault").forEach {
            context.getSharedPreferences(it, Context.MODE_PRIVATE).edit().clear().commit()
        }
        context.filesDir.resolve("courier.sqlite3").delete()
        // Selecting a folder schedules Courier work; a no-op worker keeps these tests on grants only.
        WorkManagerTestInitHelper.initializeTestWorkManager(context, Configuration.Builder()
            .setExecutor(SynchronousExecutor())
            .setWorkerFactory(object : WorkerFactory() {
                override fun createWorker(appContext: Context, workerClassName: String,
                                          workerParameters: WorkerParameters): ListenableWorker =
                    object : Worker(appContext, workerParameters) {
                        override fun doWork(): Result = Result.success()
                    }
            }).build())
        provider = TestDocumentsProvider()
        provider.attachInfo(context, ProviderInfo().apply {
            authority = this@CourierGrantIsolationTest.authority; exported = true; grantUriPermissions = true
            readPermission = "android.permission.MANAGE_DOCUMENTS"
            writePermission = "android.permission.MANAGE_DOCUMENTS"
        })
        ShadowContentResolver.registerProviderInternal(authority, provider)
        provider.docs["primary:Vault"] = TestDocumentsProvider.Doc(
            null, "Vault", DC.Document.MIME_TYPE_DIR, context.cacheDir)
    }

    @After fun teardown() {
        WorkManagerTestInitHelper.closeWorkDatabase()
    }

    private fun grant(uri: Uri) = context.contentResolver.persistedUriPermissions.singleOrNull { it.uri == uri }

    @Test fun separateVaultTreeKeepsItsGrantThroughCourierReselect() {
        VaultAccess.select(context, vaultTree)
        CourierRuntime.select(context, courierTree, rw)
        CourierRuntime.select(context, courierAlias, rw)
        assertEquals(courierAlias, CourierRuntime.tree(context))
        assertNull("old Courier grant is released", grant(courierTree))
        assertNotNull(grant(courierAlias))
        assertTrue(grant(vaultTree)!!.isReadPermission)
        assertEquals(vaultTree, VaultAccess.tree(context))
    }

    @Test fun separateVaultTreeKeepsItsGrantWhenCourierSelectionFails() {
        VaultAccess.select(context, vaultTree)
        provider.changeFinalName = true // selfTest fails after the grant was taken
        assertThrows(IllegalStateException::class.java) { CourierRuntime.select(context, courierTree, rw) }
        assertNull("failed candidate grant is released", grant(courierTree))
        assertTrue(grant(vaultTree)!!.isReadPermission)
        assertNull(CourierRuntime.tree(context))
    }

    @Test fun sharedTreeKeepsVaultGrantWhenReselectReleasesOldCourierUri() {
        VaultAccess.select(context, courierTree)
        CourierRuntime.select(context, courierTree, rw)
        CourierRuntime.select(context, courierAlias, rw)
        assertEquals(courierAlias, CourierRuntime.tree(context))
        assertTrue("vault grant on the shared tree survives", grant(courierTree)!!.isReadPermission)
        assertEquals(courierTree, VaultAccess.tree(context))
    }

    @Test fun sharedTreeKeepsVaultGrantWhenCourierSelectionFails() {
        VaultAccess.select(context, courierTree)
        provider.changeFinalName = true
        assertThrows(IllegalStateException::class.java) { CourierRuntime.select(context, courierTree, rw) }
        assertTrue("vault grant on the shared tree survives", grant(courierTree)!!.isReadPermission)
        assertNull(CourierRuntime.tree(context))
    }

    @Test fun sharedTreeVaultStillVerifiesArrivalAfterCourierReselect() {
        val bytes = "sample".toByteArray()
        val folder = provider.createDocument(TestDocumentsProvider.ROOT, DC.Document.MIME_TYPE_DIR, "wiki")
        val file = provider.createDocument(folder, "text/markdown", "a.md")
        provider.docs.getValue(file).file.writeBytes(bytes)
        VaultAccess.select(context, courierTree)
        VaultAccess.setName(context, "Example")
        CourierRuntime.select(context, courierTree, rw)
        CourierRuntime.select(context, courierAlias, rw)
        val sha = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
        assertTrue(VaultAccess.arrived(context, PipelinePage(
            path = "wiki/a.md", updatedAt = "2026-11-12T01:00:00Z", sha256 = sha)))
    }
}
