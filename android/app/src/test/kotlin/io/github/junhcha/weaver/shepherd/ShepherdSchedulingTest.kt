package io.github.junhcha.weaver.shepherd

import android.Manifest
import android.app.Application
import android.content.ContentProvider
import android.content.ContentValues
import android.content.Context
import android.database.MatrixCursor
import android.net.Uri
import androidx.test.core.app.ApplicationProvider
import androidx.work.Configuration
import androidx.work.ListenableWorker
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerFactory
import androidx.work.WorkerParameters
import androidx.work.testing.SynchronousExecutor
import androidx.work.testing.WorkManagerTestInitHelper
import java.time.ZoneId
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowContentResolver

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class ShepherdSchedulingTest {
    private lateinit var context: Context
    private lateinit var manager: WorkManager
    private var runs = 0
    private var retry = false
    @Before fun setup() {
        context = ApplicationProvider.getApplicationContext()
        ShepherdRuntime.activatedCache = null
        context.deleteDatabase("shepherd.sqlite3")
        val factory = object : WorkerFactory() {
            override fun createWorker(appContext: Context, workerClassName: String, workerParameters: WorkerParameters): ListenableWorker? {
                if (workerClassName != ShepherdWorker::class.java.name) return null
                return object : Worker(appContext, workerParameters) {
                    override fun doWork(): Result { runs++; return if (retry) Result.retry() else Result.success() }
                }
            }
        }
        WorkManagerTestInitHelper.initializeTestWorkManager(context, Configuration.Builder()
            .setExecutor(SynchronousExecutor()).setWorkerFactory(factory).build())
        manager = WorkManager.getInstance(context)
    }
    @After fun close() {
        manager.cancelAllWork().result.get()
        WorkManagerTestInitHelper.closeWorkDatabase()
    }
    @Test fun foregroundCallbacksKeepOnlyOneOutstandingKick() {
        retry = true
        shadowOf(context as Application).grantPermissions(Manifest.permission.READ_MEDIA_AUDIO)
        val observer = ShepherdRuntime.observeForeground(context)!!
        try {
            repeat(20) { observer.onChange(false) }
            val work = manager.getWorkInfosForUniqueWork("shepherd-kick").get()
            assertEquals(1, work.count { !it.state.isFinished })
            assertEquals(1, runs)
        } finally { context.contentResolver.unregisterContentObserver(observer) }
    }
    @Test fun synchronousScanFindsEarlySharedTranscriptAndReservesProcessing() {
        val provider = object : ContentProvider() {
            override fun onCreate() = true
            override fun query(uri: Uri, projection: Array<out String>?, selection: String?, args: Array<out String>?, sort: String?) =
                MatrixCursor(projection!!).apply { addRow(arrayOf<Any>(1L, "Person_261108_123456.m4a", 100L,
                    System.currentTimeMillis() / 1000, System.currentTimeMillis() / 1000, "Recordings/Call/")) }
            override fun getType(uri: Uri): String? = null
            override fun insert(uri: Uri, values: ContentValues?): Uri? = error("No media writes")
            override fun delete(uri: Uri, selection: String?, args: Array<out String>?): Int = error("No media writes")
            override fun update(uri: Uri, values: ContentValues?, selection: String?, args: Array<out String>?): Int = error("No media writes")
        }
        ShadowContentResolver.registerProviderInternal("media", provider)
        shadowOf(context as Application).grantPermissions(Manifest.permission.READ_MEDIA_AUDIO)
        ShepherdDatabase(context).use { it.activate(System.currentTimeMillis() - 10_000) }
        ShepherdRuntime.scanNow(context)
        val call = ShepherdRuntime.pendingCalls(context).single()
        assertTrue(ShepherdRuntime.calls(context).isEmpty())
        assertTrue(ShepherdRuntime.markProcessed(context, call.id))
        assertFalse(ShepherdRuntime.markProcessed(context, call.id))
        // Reuse the exact provider metadata to avoid a clock-second boundary changing modified.
        ShepherdDatabase(context).use { db ->
            val metadata = RecordingScanner(context).scan(db.activatedAt()!!)
            db.observe(metadata, System.currentTimeMillis(), ZoneId.of("UTC"))
            db.observe(metadata, System.currentTimeMillis() + 30_000, ZoneId.of("UTC"))
            assertEquals(CallStatus.PROCESSED, db.calls().single().status)
        }
        assertTrue(ShepherdRuntime.pendingCalls(context).isEmpty())
    }
    @Test fun noWorkBeforeOptInAndRepeatedResumeKeepsOnePeriodicJobWithFreshScan() {
        ShepherdRuntime.schedule(context)
        assertTrue(manager.getWorkInfosForUniqueWork("shepherd-periodic").get().isEmpty())
        ShepherdRuntime.activate(context)
        val activation = ShepherdDatabase(context).use { it.activatedAt() }
        val beforeResume = runs
        ShepherdRuntime.schedule(context)
        assertTrue(runs > beforeResume)
        val periodic = manager.getWorkInfosForUniqueWork("shepherd-periodic").get()
        assertEquals(1, periodic.size)
        assertEquals(15 * 60 * 1000L, periodic.single().periodicityInfo!!.repeatIntervalMillis)
        ShepherdRuntime.activate(context)
        assertEquals(activation, ShepherdDatabase(context).use { it.activatedAt() })
    }
}
