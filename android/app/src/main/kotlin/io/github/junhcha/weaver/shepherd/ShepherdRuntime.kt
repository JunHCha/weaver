package io.github.junhcha.weaver.shepherd

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.database.ContentObserver
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import io.github.junhcha.weaver.MainActivity
import io.github.junhcha.weaver.R
import io.github.junhcha.weaver.shepherd.autoshare.AutoShareRuntime
import io.github.junhcha.weaver.ui.Feature
import java.time.Instant
import java.time.ZoneId
import java.util.concurrent.TimeUnit

/** Synchronous methods run on an IO thread. Audio is never opened, copied, hashed or sent to Courier. */
object ShepherdRuntime {
    internal val lock = Any()
    @Volatile internal var activatedCache: Boolean? = null
    const val OPEN_PENDING = "io.github.junhcha.weaver.OPEN_PENDING_CALLS"
    const val REMINDER_HOUR = 21
    private const val CHANNEL = "shepherd-calls"
    private const val NOTIFICATION_ID = 30

    fun activate(context: Context) {
        synchronized(lock) { ShepherdDatabase(context).use { it.activate(System.currentTimeMillis()); activatedCache = true } }
        schedule(context)
    }
    fun isActivated(context: Context): Boolean = activatedCache ?: synchronized(lock) {
        activatedCache ?: ShepherdDatabase(context).use {
            (it.activatedAt() != null).also { activatedCache = it }
        }
    }
    fun pendingCalls(context: Context): List<CallRecord> = synchronized(lock) {
        ShepherdDatabase(context).use { it.pendingCalls() }
    }
    fun calls(context: Context): List<CallRecord> = synchronized(lock) {
        ShepherdDatabase(context).use { it.calls() }
    }
    /** #31 calls this after durable transcript enqueue; idempotent and does not request permissions. */
    fun markProcessed(context: Context, id: String): Boolean = synchronized(lock) {
        ShepherdDatabase(context).use { db ->
            val changed = db.markProcessed(id)
            if (changed) notifyPending(context, db, System.currentTimeMillis())
            changed
        }
    }
    /** Explicit user choice to stop reminding about a stable unshared call. Call from IO. */
    fun dismiss(context: Context, id: String): Boolean = synchronized(lock) {
        ShepherdDatabase(context).use { db ->
            db.dismiss(id).also { if (it) notifyPending(context, db, System.currentTimeMillis()) }
        }
    }
    fun schedule(context: Context) {
        if (!isActivated(context)) return
        WorkManager.getInstance(context).enqueueUniquePeriodicWork("shepherd-periodic", ExistingPeriodicWorkPolicy.KEEP,
            PeriodicWorkRequestBuilder<ShepherdWorker>(15, TimeUnit.MINUTES).build())
        kick(context)
    }
    internal fun kick(context: Context) {
        WorkManager.getInstance(context).enqueueUniqueWork("shepherd-kick", ExistingWorkPolicy.KEEP,
            OneTimeWorkRequestBuilder<ShepherdWorker>().addTag("shepherd-scan").build())
    }
    /** Activity owns this observer, unregistering on pause; callbacks only enqueue metadata work. */
    fun observeForeground(context: Context): ContentObserver? {
        if (!Feature.SHEPHERD.isGranted(context)) return null
        val app = context.applicationContext
        val observer = object : ContentObserver(Handler(Looper.getMainLooper())) {
            override fun onChange(selfChange: Boolean) { kick(app) }
        }
        return try {
            app.contentResolver.registerContentObserver(MediaStore.Audio.Media.EXTERNAL_CONTENT_URI, true, observer)
            observer
        } catch (_: SecurityException) { null }
    }
    /** #31 scans synchronously before matching a shared transcript. Call from IO. */
    fun scanNow(context: Context) {
        if (scan(context)) scheduleStability(context)
    }
    internal fun scheduleStability(context: Context) {
        WorkManager.getInstance(context).enqueueUniqueWork("shepherd-stability", ExistingWorkPolicy.APPEND_OR_REPLACE,
            OneTimeWorkRequestBuilder<ShepherdWorker>().setInitialDelay(30, TimeUnit.SECONDS).build())
    }
    internal fun scan(context: Context, now: Long = System.currentTimeMillis()): Boolean = synchronized(lock) {
        ShepherdDatabase(context).use { db ->
            val activated = db.activatedAt() ?: return@synchronized false
            var pendingObservation = false
            if (Feature.SHEPHERD.isGranted(context)) {
                try {
                    pendingObservation = db.observe(RecordingScanner(context).scan(activated), now, ZoneId.systemDefault())
                    db.setting("permission_missing", "false")
                } catch (_: SecurityException) { db.setting("permission_missing", "true") }
            } else { db.setting("permission_missing", "true") }
            notifyPending(context, db, now)
            // Same run as the call reminder: the 21:00 review reminder (once per local day) fires here too.
            runCatching { io.github.junhcha.weaver.inbox.InboxRuntime.refresh(context, now) }
            pendingObservation
        }
    }

    @Suppress("MissingPermission") // Notification permission and enabled channel are checked immediately before notify.
    internal fun notifyPending(context: Context, db: ShepherdDatabase, now: Long) {
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel(CHANNEL, "미공유 통화", NotificationManager.IMPORTANCE_DEFAULT))
        val unshared = db.calls().filter { it.status == CallStatus.UNSHARED }.map { it.id }
        // Calls auto share has handled, or is still trying to handle, are not announced for manual sharing.
        val silenced = AutoShareRuntime.silenced(context, db, unshared)
        val ids = unshared.filter { it !in silenced }.sorted()
        if (ids.isEmpty()) {
            manager.cancel(NOTIFICATION_ID)
            db.setting("notified_ids", "")
            return
        }
        if (!Feature.NOTIFICATIONS.isGranted(context) || !NotificationManagerCompat.from(context).areNotificationsEnabled() ||
            manager.getNotificationChannel(CHANNEL)?.importance == NotificationManager.IMPORTANCE_NONE) return
        val signature = ids.joinToString("\n")
        val old = db.setting("notified_ids").orEmpty().split('\n').filter { it.isNotEmpty() }
        val local = Instant.ofEpochMilli(now).atZone(ZoneId.systemDefault())
        val day = local.toLocalDate().toString()
        val added = ids.any { it !in old }
        val reminder = local.hour >= REMINDER_HOUR && db.setting("reminded_day") != day
        if (signature == db.setting("notified_ids") && !reminder) return
        if (!added && !reminder && manager.activeNotifications.none { it.id == NOTIFICATION_ID }) {
            db.setting("notified_ids", signature)
            return
        }
        val intent = PendingIntent.getActivity(context, NOTIFICATION_ID,
            Intent(context, MainActivity::class.java).setAction(OPEN_PENDING)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        try {
            manager.notify(NOTIFICATION_ID, NotificationCompat.Builder(context, CHANNEL)
                .setSmallIcon(R.drawable.ic_launcher_foreground).setContentTitle("스크립트 미공유 통화 ${ids.size}건")
                .setContentText("갤럭시 녹음 앱에서 스크립트를 Weaver로 공유하세요")
                .setContentIntent(intent).setAutoCancel(true).setSilent(!added && !reminder).build())
            db.setting("notified_ids", signature)
            if (reminder) db.setting("reminded_day", day)
        } catch (_: SecurityException) { /* Permission revoked between check and notify; retry on next scan. */ }
    }
}

class ShepherdWorker(context: Context, params: WorkerParameters) : Worker(context, params) {
    override fun doWork(): Result = try {
        if (ShepherdRuntime.scan(applicationContext)) {
            ShepherdRuntime.scheduleStability(applicationContext)
        }
        runCatching { AutoShareRuntime.maybeStart(applicationContext) }
        Result.success()
    } catch (_: Exception) { Result.retry() }
}
