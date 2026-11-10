package io.github.junhcha.weaver.widget

import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProviderInfo
import android.content.ComponentName
import android.content.Context
import androidx.glance.appwidget.AppWidgetId
import androidx.test.core.app.ApplicationProvider
import androidx.work.Configuration
import androidx.work.ListenableWorker
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerFactory
import androidx.work.WorkerParameters
import androidx.work.testing.SynchronousExecutor
import androidx.work.testing.WorkManagerTestInitHelper
import io.github.junhcha.weaver.courier.MemoryFolder
import io.github.junhcha.weaver.inbox.InboxDatabase
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.review.ReviewDraft
import io.github.junhcha.weaver.review.ReviewStore
import io.github.junhcha.weaver.schema.*
import io.github.junhcha.weaver.storage.AppStorage
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.io.File

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class WidgetIntegrationTest {
    private lateinit var context: Context
    private val p = WeaverCodec.json.decodeFromString<ReviewPackage>(File(SharedSchema.dir, "examples/ReviewPackage.json").readText())
    private val alert = WeaverCodec.json.decodeFromString<Alert>(File(SharedSchema.dir, "examples/Alert.json").readText())
    private val folder = MemoryFolder()
    private val snapshots = mutableMapOf<Int, WidgetModel>()
    private val now = System.currentTimeMillis()

    @Before fun setup() {
        context = ApplicationProvider.getApplicationContext()
        File(context.filesDir, "inbox.sqlite3").delete()
        File(AppStorage.exchangeDir(context), "reviews").deleteRecursively()
        context.getSharedPreferences("courier", Context.MODE_PRIVATE).edit().clear().commit()
        context.getSharedPreferences("widget-sync", Context.MODE_PRIVATE).edit().clear().commit()
        val component = ComponentName(context, ReviewWidgetReceiver::class.java)
        val info = AppWidgetProviderInfo().apply { provider = component }
        val manager = shadowOf(AppWidgetManager.getInstance(context))
        manager.addInstalledProvider(info)
        manager.addBoundWidget(101, info)
        manager.addBoundWidget(202, info)
        val factory = object : WorkerFactory() {
            override fun createWorker(appContext: Context, workerClassName: String, workerParameters: WorkerParameters): ListenableWorker? {
                if (workerClassName != WidgetUpdateWorker::class.java.name) return null
                return object : Worker(appContext, workerParameters) {
                    override fun doWork(): Result {
                        // Exercise production instance enumeration; replace RemoteViews publication only.
                        runBlocking { WidgetUpdates.updateInstances(appContext) {
                            snapshots[(it as AppWidgetId).appWidgetId] = WidgetModel.load(appContext, now)
                        } }
                        return Result.success()
                    }
                }
            }
        }
        WorkManagerTestInitHelper.initializeTestWorkManager(context, Configuration.Builder()
            .setWorkerFactory(factory).setExecutor(SynchronousExecutor()).build())
    }
    @After fun cleanup() { WorkManagerTestInitHelper.closeWorkDatabase() }
    private fun reconcile(items: List<ReviewPackage>, alerts: List<Alert> = emptyList(), time: Long = now) {
        InboxDatabase(context).use { it.reconcile(items, alerts, emptySet(), time, folder.identity, false) }
        InboxRuntime.refresh(context, time)
        WorkManager.getInstance(context).getWorkInfosForUniqueWork("review-widget-refresh").get()
    }
    @Test fun updatesDeletionAlertsAndQueuedVerdictsReachBothInstances() {
        reconcile(listOf(p, p.copy(revision = 2)))
        assertEquals(setOf(101, 202), snapshots.keys)
        assertTrue(snapshots.values.all { it.pendingCount == 1 && it.rows.single().revision == 2L })
        reconcile(listOf(p.copy(revision = 3)), listOf(alert), now + 1000)
        assertTrue(snapshots.values.all { it.rows.single().revision == 3L && it.warning(false) != null })
        val newest = p.copy(revision = 3)
        folder.put("proposals", "${p.packageId}.r3.json", WeaverCodec.encode(newest))
        val verdict = ReviewDraft.verdict(newest, listOf("홍길동"), Verdict.Action.APPROVE, "", "")
        ReviewStore(File(AppStorage.exchangeDir(context), "reviews")).queue(folder, newest, verdict)
        InboxRuntime.refresh(context)
        WorkManager.getInstance(context).getWorkInfosForUniqueWork("review-widget-refresh").get()
        assertTrue(snapshots.values.all { it.pendingCount == 0 && it.rows.single().submitted })
        reconcile(emptyList())
        assertTrue(snapshots.values.all { it.rows.isEmpty() && it.alertMessages.isEmpty() })
        // A process restart/new reader still sees the durable local deletion.
        assertTrue(WidgetModel.load(context, now).rows.isEmpty())
    }
    @Test fun revisionArrivalSortAndFreshWaitingDelayedErrorStates() {
        assertTrue(WidgetModel.load(context, now).status.contains("동기화 대기"))
        val other = p.copy(packageId = "pkg_01ARZ3NDEKTSV4RRFFQ69G5FA1")
        reconcile(listOf(p), time = now - 5000)
        reconcile(listOf(p, other), time = now - 3000)
        assertEquals(other.packageId, WidgetModel.load(context, now).rows.first().id)
        reconcile(listOf(p.copy(revision = 2), other), time = now - 1000)
        assertEquals(p.packageId, WidgetModel.load(context, now).rows.first().id)
        assertTrue(WidgetModel.load(context, now).status.contains("최근 검사 완료"))
        assertTrue(WidgetModel.load(context, now + 31 * 60_000).status.contains("동기화 지연"))
        WidgetUpdates.scanFinished(context, false)
        assertTrue(WidgetModel.load(context, now).status.contains("동기화 오류"))
        assertEquals(2, WidgetModel.load(context, now).pendingCount)
        WidgetUpdates.scanFinished(context, true)
        assertTrue(WidgetModel.load(context, now).status.contains("최근 검사 완료"))
        context.getSharedPreferences("courier", Context.MODE_PRIVATE).edit().putBoolean("stopped", true).commit()
        assertTrue(WidgetModel.load(context, now).status.contains("동기화 오류"))
    }
    @Test fun clickRechecksTheRealReviewStoreAfterRevisionChangeAndRemoval() {
        val store = ReviewStore(File(AppStorage.exchangeDir(context), "reviews"))
        folder.put("proposals", "${p.packageId}.r1.json", WeaverCodec.encode(p))
        folder.put("proposals", "${p.packageId}.r4.json", WeaverCodec.encode(p.copy(revision = 4)))
        val target = WidgetReviewTarget.resolve(p.packageId, 1) { store.latest(folder, it).revision }
        assertEquals(WidgetReviewTarget.Ready(p.packageId, 1, 4), target)
        folder.files.clear()
        val gone = WidgetReviewTarget.resolve(p.packageId, 1) { store.latest(folder, it).revision }
        assertTrue((gone as WidgetReviewTarget.Unavailable).message.contains("사라졌습니다"))
    }
    @Test fun verdictFromAnotherTreeDoesNotRemovePendingCount() {
        reconcile(listOf(p))
        folder.put("proposals", "${p.packageId}.r1.json", WeaverCodec.encode(p))
        folder.root = "memory://another-tree"
        val verdict = ReviewDraft.verdict(p, listOf("홍길동"), Verdict.Action.APPROVE, "", "")
        ReviewStore(File(AppStorage.exchangeDir(context), "reviews")).queue(folder, p, verdict)
        assertEquals(1, WidgetModel.load(context).pendingCount)
    }
    @Test fun widgetClickIntentsAreIsolatedByIdAndRevision() {
        val first = WidgetReviewActivity.intent(context, p.packageId, 1)
        val newer = WidgetReviewActivity.intent(context, p.packageId, 2)
        assertFalse(first.filterEquals(newer))
        assertEquals(p.packageId, first.getStringExtra(WidgetReviewActivity.PACKAGE_ID))
        assertEquals(1L, first.getLongExtra(WidgetReviewActivity.REVISION, 0))
        assertEquals(2L, newer.getLongExtra(WidgetReviewActivity.REVISION, 0))
    }
}
