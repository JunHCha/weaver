package io.github.junhcha.weaver.transport

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.work.Configuration
import androidx.work.WorkManager
import androidx.work.testing.SynchronousExecutor
import androidx.work.testing.TestListenableWorkerBuilder
import androidx.work.testing.WorkManagerTestInitHelper
import io.github.junhcha.weaver.diagnostics.CrashDiagnostics
import io.github.junhcha.weaver.diagnostics.CrashReport
import io.github.junhcha.weaver.diagnostics.WeaverApplication
import kotlinx.serialization.json.Json
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class CrashStartupTest {
    @Test fun applicationStartSchedulesPendingCrashAndWorkerSendsEvenWithFilesRoutes() {
        val app = ApplicationProvider.getApplicationContext<WeaverApplication>()
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        val oldBox = TransportRuntime.secretBox
        val oldFactory = TransportRuntime.apiFactory
        WorkManagerTestInitHelper.initializeTestWorkManager(app, Configuration.Builder().setExecutor(SynchronousExecutor()).build())
        val manager = WorkManager.getInstance(app)
        try {
            TransportRuntime.secretBox = { ReverseBox() }
            app.getSharedPreferences("transport", Context.MODE_PRIVATE).edit().clear().commit()
            app.getSharedPreferences("courier", Context.MODE_PRIVATE).edit().clear().putBoolean("stopped", true).commit()
            val store = CrashDiagnostics.store(app)
            val report = Json.decodeFromString<CrashReport>(ContractFixtures["crashes.stored"].requestBody.toString())
            store.save(report)
            assertTrue(java.io.File(app.noBackupFilesDir, "crashes").isDirectory)
            app.onCreate()
            assertTrue(manager.getWorkInfosByTag("transport-immediate").get().isEmpty())
            assertEquals(1, store.pending().size)
            FakeMobileServer().use { server ->
                TransportRuntime.settings(app).pair(FakeMobileServer.pairing(server))
                assertFalse(TransportRuntime.settings(app).anyHttp)
                server.serve("crashes.stored")
                // The next application start installs the handler and kicks a CONNECTED worker.
                app.onCreate()
                assertEquals(1, manager.getWorkInfosByTag("transport-immediate").get().size)
                TestListenableWorkerBuilder<TransportWorker>(app).build().doWork()
                assertTrue(store.pending().isEmpty())
                assertNotNull(store.lastSummary())
                assertEquals(1, server.count("/diagnostics/crashes"))
                assertTrue(server.mismatches.toString(), server.mismatches.isEmpty())
            }
        } finally {
            Thread.setDefaultUncaughtExceptionHandler(previous)
            TransportRuntime.secretBox = oldBox
            TransportRuntime.apiFactory = oldFactory
            manager.cancelAllWork().result.get()
            WorkManagerTestInitHelper.closeWorkDatabase()
            java.io.File(app.noBackupFilesDir, "crashes").deleteRecursively()
            app.getSharedPreferences("transport", Context.MODE_PRIVATE).edit().clear().commit()
            app.getSharedPreferences("courier", Context.MODE_PRIVATE).edit().clear().commit()
        }
    }
}
