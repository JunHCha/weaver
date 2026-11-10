package io.github.junhcha.weaver.widget

import android.appwidget.AppWidgetManager
import android.content.ComponentName
import android.content.Context
import androidx.glance.GlanceId
import androidx.glance.appwidget.GlanceAppWidgetManager
import androidx.work.*
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow

/** Durable update work refreshes every installed instance, including after process restart. */
object WidgetUpdates {
    internal val changes = MutableStateFlow(0L)
    private fun prefs(context: Context) = context.getSharedPreferences("widget-sync", Context.MODE_PRIVATE)
    fun scanFailed(context: Context) = prefs(context).getBoolean("failed", false)
    fun scanFinished(context: Context, successful: Boolean) {
        prefs(context).edit().putBoolean("failed", !successful).commit()
        request(context)
    }
    internal suspend fun updateInstances(context: Context, render: suspend (GlanceId) -> Unit = {
        ReviewWidget().update(context, it)
    }) {
        GlanceAppWidgetManager(context).getGlanceIds(ReviewWidget::class.java).forEach { render(it) }
    }
    fun request(context: Context) {
        changes.value += 1
        if (AppWidgetManager.getInstance(context).getAppWidgetIds(
                ComponentName(context, ReviewWidgetReceiver::class.java)).isEmpty()) return
        WorkManager.getInstance(context).enqueueUniqueWork("review-widget-refresh",
            ExistingWorkPolicy.APPEND_OR_REPLACE, OneTimeWorkRequestBuilder<WidgetUpdateWorker>().build())
    }
}
class WidgetUpdateWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result = try {
        WidgetUpdates.updateInstances(applicationContext)
        Result.success()
    } catch (cancelled: CancellationException) { throw cancelled
    } catch (_: Exception) { Result.retry() }
}
