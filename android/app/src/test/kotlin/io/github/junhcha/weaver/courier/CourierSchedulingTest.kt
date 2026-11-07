package io.github.junhcha.weaver.courier

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.work.Clock
import androidx.work.Configuration
import androidx.work.ListenableWorker
import androidx.work.WorkInfo
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerFactory
import androidx.work.WorkerParameters
import androidx.work.testing.SynchronousExecutor
import androidx.work.testing.WorkManagerTestInitHelper
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class CourierSchedulingTest {
    private lateinit var context: Context
    private lateinit var workManager: WorkManager
    private val executions = mutableListOf<UUID>()
    private var retryId: UUID? = null
    private val now = System.currentTimeMillis()

    @Before fun setup() {
        context = ApplicationProvider.getApplicationContext()
        context.getSharedPreferences("courier", Context.MODE_PRIVATE).edit()
            .clear().putString("tree", "content://synthetic/tree/courier").commit()
        val factory = object : WorkerFactory() {
            override fun createWorker(
                appContext: Context,
                workerClassName: String,
                workerParameters: WorkerParameters,
            ): ListenableWorker? {
                if (workerClassName != CourierWorker::class.java.name) return null
                // Exercise the production enqueue path without touching SAF or user data.
                return object : Worker(appContext, workerParameters) {
                    override fun doWork(): Result {
                        executions.add(id)
                        if (retryId == null) retryId = id
                        return if (id == retryId) Result.retry() else Result.success()
                    }
                }
            }
        }
        WorkManagerTestInitHelper.initializeTestWorkManager(context, Configuration.Builder()
            .setExecutor(SynchronousExecutor())
            .setClock(object : Clock { override fun currentTimeMillis() = now })
            .setWorkerFactory(factory)
            .build())
        workManager = WorkManager.getInstance(context)
    }

    @After fun teardown() {
        workManager.cancelAllWork().result.get()
        WorkManagerTestInitHelper.closeWorkDatabase()
        context.getSharedPreferences("courier", Context.MODE_PRIVATE).edit().clear().commit()
    }

    @Test fun freshKickRunsWhilePreviousKickWaitsForRetryBackoff() {
        CourierRuntime.kick(context)
        val waiting = workManager.getWorkInfosByTag(CourierWorker::class.java.name).get().single()
        assertEquals(listOf(waiting.id), executions)
        assertEquals(WorkInfo.State.ENQUEUED, waiting.state)
        assertEquals(1, waiting.runAttemptCount)
        assertTrue("The first request must be waiting for backoff", waiting.nextScheduleTimeMillis > now)

        // The same entry point serves capture preparation, foreground and folder re-selection.
        CourierRuntime.kick(context)
        val requests = workManager.getWorkInfosByTag(CourierWorker::class.java.name).get()
        assertEquals(2, requests.size)
        val fresh = requests.single { it.id != waiting.id }
        assertEquals(WorkInfo.State.SUCCEEDED, fresh.state)
        assertEquals(listOf(waiting.id, fresh.id), executions)
        val stillWaiting = requests.single { it.id == waiting.id }
        assertEquals(WorkInfo.State.ENQUEUED, stillWaiting.state)
        assertEquals(waiting.nextScheduleTimeMillis, stillWaiting.nextScheduleTimeMillis)
    }
}
