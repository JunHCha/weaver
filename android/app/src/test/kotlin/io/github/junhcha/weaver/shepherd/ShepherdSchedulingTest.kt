package io.github.junhcha.weaver.shepherd

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.work.Configuration
import androidx.work.ListenableWorker
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerFactory
import androidx.work.WorkerParameters
import androidx.work.testing.SynchronousExecutor
import androidx.work.testing.WorkManagerTestInitHelper
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class ShepherdSchedulingTest {
    private lateinit var context: Context
    private lateinit var manager: WorkManager
    private var runs = 0
    @Before fun setup() {
        context = ApplicationProvider.getApplicationContext()
        context.deleteDatabase("shepherd.sqlite3")
        val factory = object : WorkerFactory() {
            override fun createWorker(appContext: Context, workerClassName: String, workerParameters: WorkerParameters): ListenableWorker? {
                if (workerClassName != ShepherdWorker::class.java.name) return null
                return object : Worker(appContext, workerParameters) {
                    override fun doWork(): Result { runs++; return Result.success() }
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
