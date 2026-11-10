package io.github.junhcha.weaver.inbox

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import io.github.junhcha.weaver.MainActivity
import io.github.junhcha.weaver.R
import io.github.junhcha.weaver.courier.CourierClient
import io.github.junhcha.weaver.courier.CourierDatabase
import io.github.junhcha.weaver.courier.CourierFolder
import io.github.junhcha.weaver.courier.CourierRuntime
import io.github.junhcha.weaver.shepherd.ShepherdRuntime
import io.github.junhcha.weaver.ui.Feature
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import java.time.Instant
import java.time.ZoneId

/**
 * New proposals and change alerts from Courier proposals/. Runs inside CourierWorker (same 15-minute
 * unique periodic work and foreground kick); never requests a permission and never writes the calendar.
 * Synchronous methods run on an IO thread.
 */
object InboxRuntime {
    internal val lock = Any()
    const val OPEN_INBOX = "io.github.junhcha.weaver.OPEN_INBOX"
    /** Shown to the user: the Courier periodic interval. Doze/battery optimisation can delay it further. */
    const val INTERVAL_MINUTES = 15L
    internal const val PROPOSAL_CHANNEL = "inbox-proposals"
    internal const val ALERT_CHANNEL = "inbox-alerts"
    internal const val PROPOSAL_NOTIFICATION = 33
    internal const val ALERT_NOTIFICATION = 34
    private const val MAX_LINES = 3

    private val version = MutableStateFlow(0L)
    /** Bumps whenever the local list changes; UI observes it; WidgetUpdates refreshes installed widget instances. */
    val changes: StateFlow<Long> get() = version

    /**
     * Apply a complete Courier scan. Returns false when an alert file could not be deleted so WorkManager
     * retries; the local state is already committed and deletion is retried on every scan.
     */
    fun apply(context: Context, folder: CourierFolder, inbox: CourierClient.Inbox, diagnostics: CourierDatabase,
              now: Long = System.currentTimeMillis()): Boolean {
        val delete = synchronized(lock) {
            InboxDatabase(context).use {
                it.reconcile(inbox.proposals, inbox.alerts, inbox.unreadable, now, folder.identity, folder.created("proposals"), inbox.packages)
            }
        }
        var successful = true
        val done = mutableListOf<String>()
        try {
            delete.forEach { name ->
                try { folder.delete("proposals", name); done.add(name)
                } catch (error: SecurityException) { throw error
                } catch (error: Exception) {
                    diagnostics.diagnostic("proposals", name, CourierClient.errorCode(error)); successful = false
                }
            }
        } finally {
            if (done.isNotEmpty()) synchronized(lock) { InboxDatabase(context).use { it.deleted(done) } }
        }
        refresh(context, now)
        return successful
    }

    fun proposals(context: Context): List<InboxDatabase.ProposalItem> = synchronized(lock) {
        InboxDatabase(context).use { it.proposals() }
    }
    fun alerts(context: Context): List<InboxDatabase.AlertItem> = synchronized(lock) {
        InboxDatabase(context).use { it.alerts() }
    }
    fun lastScanAt(context: Context): Long? = synchronized(lock) { InboxDatabase(context).use { it.lastScanAt() } }

    /** Called when this app queues a verdict, so a resynced copy of that rev never reappears in the list. */
    fun submitted(context: Context, proposalId: String, rev: Long) = synchronized(lock) {
        InboxDatabase(context).use { it.submitted(proposalId, rev) }
    }

    /** User closes an alert: it leaves the list now, and the next Courier run deletes its file. */
    fun dismissAlert(context: Context, alertId: String, now: Long = System.currentTimeMillis()): Boolean {
        val changed = synchronized(lock) { InboxDatabase(context).use { it.dismiss(alertId, now) } }
        if (changed) {
            refresh(context, now)
            CourierRuntime.kick(context)
        }
        return changed
    }

    /** Update both summary notifications. Also called from ShepherdWorker so the 21:00 reminders share a run. */
    fun refresh(context: Context, now: Long = System.currentTimeMillis()) {
        synchronized(lock) {
            InboxDatabase(context).use { db ->
                notifyProposals(context, db, now)
                notifyAlerts(context, db)
            }
        }
        version.value = version.value + 1
        io.github.junhcha.weaver.widget.WidgetUpdates.request(context)
    }

    private fun manager(context: Context): NotificationManager {
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel(PROPOSAL_CHANNEL, "검토 대기 제안", NotificationManager.IMPORTANCE_DEFAULT))
        manager.createNotificationChannel(NotificationChannel(ALERT_CHANNEL, "변경 경고", NotificationManager.IMPORTANCE_DEFAULT))
        return manager
    }

    private fun allowed(context: Context, manager: NotificationManager, channel: String): Boolean =
        Feature.NOTIFICATIONS.isGranted(context) && NotificationManagerCompat.from(context).areNotificationsEnabled() &&
            manager.getNotificationChannel(channel)?.importance != NotificationManager.IMPORTANCE_NONE

    /**
     * Sound only for a proposal_id never notified before (or the daily 21:00 reminder). A new rev of a
     * known proposal re-posts silently. Removals only update a still-visible summary. While notifications
     * are blocked nothing is marked notified; the first post after unblocking is silent.
     */
    internal fun notifyProposals(context: Context, db: InboxDatabase, now: Long) {
        val manager = manager(context)
        val items = db.proposals()
        if (items.isEmpty()) {
            manager.cancel(PROPOSAL_NOTIFICATION)
            db.setting("proposal_signature", "")
            return
        }
        val added = items.any { it.notifiedRev == 0L }
        val revised = items.any { it.notifiedRev in 1 until it.rev }
        if (!allowed(context, manager, PROPOSAL_CHANNEL)) {
            if (added || revised) db.setting("proposal_blocked", "true")
            return
        }
        val local = Instant.ofEpochMilli(now).atZone(ZoneId.systemDefault())
        val day = local.toLocalDate().toString()
        val reminder = local.hour >= ShepherdRuntime.REMINDER_HOUR && db.setting("reminded_day") != day
        val signature = items.joinToString("\n") { "${it.proposalId}.r${it.rev}" }
        val visible = manager.activeNotifications.any { it.id == PROPOSAL_NOTIFICATION }
        if (!added && !revised && !reminder && !(visible && signature != db.setting("proposal_signature"))) {
            db.setting("proposal_signature", signature)
            return
        }
        val blocked = db.setting("proposal_blocked") == "true"
        val lines = items.map { it.summary.take(80) + if (it.revised) " · 다시 짠 제안" else "" }
        if (post(context, manager, PROPOSAL_NOTIFICATION, PROPOSAL_CHANNEL, "검토 대기 제안 ${items.size}건", lines,
                silent = blocked || !(added || reminder))) {
            db.markProposalsNotified()
            db.setting("proposal_signature", signature)
            db.setting("proposal_blocked", "false")
            if (reminder) db.setting("reminded_day", day)
        }
    }

    /** Sound only for a new alert_id; no reminder. Tapping or swiping is "확인" and is not resurrected. */
    internal fun notifyAlerts(context: Context, db: InboxDatabase) {
        val manager = manager(context)
        val items = db.alerts()
        if (items.isEmpty()) {
            manager.cancel(ALERT_NOTIFICATION)
            db.setting("alert_signature", "")
            return
        }
        val added = items.any { !it.notified }
        if (!allowed(context, manager, ALERT_CHANNEL)) {
            if (added) db.setting("alert_blocked", "true")
            return
        }
        val signature = items.joinToString("\n") { it.alertId }
        val visible = manager.activeNotifications.any { it.id == ALERT_NOTIFICATION }
        if (!added && !(visible && signature != db.setting("alert_signature"))) {
            db.setting("alert_signature", signature)
            return
        }
        val blocked = db.setting("alert_blocked") == "true"
        val lines = items.map { "승인 전 · ${it.message.take(80)}" }
        if (post(context, manager, ALERT_NOTIFICATION, ALERT_CHANNEL, "일정 변경 가능 ${items.size}건 · 승인 전", lines,
                silent = blocked || !added)) {
            db.markAlertsNotified()
            db.setting("alert_signature", signature)
            db.setting("alert_blocked", "false")
        }
    }

    /**
     * Lock screen and the public version show only the count title. Unlocked: up to three lines.
     * setLocalOnly keeps summaries off bridged wearables. Script excerpts never enter a notification.
     */
    @Suppress("MissingPermission") // allowed() checks permission and channel immediately before notify.
    private fun post(context: Context, manager: NotificationManager, id: Int, channel: String, title: String,
                     lines: List<String>, silent: Boolean): Boolean {
        val intent = PendingIntent.getActivity(context, id,
            Intent(context, MainActivity::class.java).setAction(OPEN_INBOX)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val public = NotificationCompat.Builder(context, channel).setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle(title).setContentIntent(intent).setLocalOnly(true).build()
        val style = NotificationCompat.InboxStyle().setBigContentTitle(title)
        lines.take(MAX_LINES).forEach(style::addLine)
        if (lines.size > MAX_LINES) style.setSummaryText("외 ${lines.size - MAX_LINES}건")
        return try {
            manager.notify(id, NotificationCompat.Builder(context, channel)
                .setSmallIcon(R.drawable.ic_launcher_foreground).setContentTitle(title).setContentText(lines.first())
                .setStyle(style).setNumber(lines.size)
                .setVisibility(NotificationCompat.VISIBILITY_PRIVATE).setPublicVersion(public).setLocalOnly(true)
                .setContentIntent(intent).setAutoCancel(true).setSilent(silent).build())
            true
        } catch (_: SecurityException) { false /* Revoked between check and notify; next scan retries. */ }
    }
}
