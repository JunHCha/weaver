package io.github.junhcha.weaver.review

import android.content.Context
import androidx.work.*
import io.github.junhcha.weaver.courier.CourierFolder
import io.github.junhcha.weaver.courier.CourierRuntime
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.schema.ReviewPackage
import io.github.junhcha.weaver.schema.Verdict
import io.github.junhcha.weaver.storage.AppStorage
import java.io.File
import java.util.concurrent.TimeUnit

/** IO entry points; serializes UI actions and WorkManager with Courier's process lock. */
object ReviewRuntime {
    private fun store(context: Context) = ReviewStore(File(AppStorage.exchangeDir(context), "reviews"))
    fun records(context: Context): List<ReviewRecord> = synchronized(CourierRuntime.lock) { store(context).records() }
    fun open(context: Context, id: String): ReviewPackage = synchronized(CourierRuntime.lock) {
        val p = store(context).latest(CourierRuntime.routed(context), id)
        val known = InboxRuntime.reviews(context).firstOrNull { it.packageId == id }
        check(known == null || p.revision >= known.revision) { "최신 revision의 동기화를 기다리세요" }
        p
    }
    fun submit(context: Context, p: ReviewPackage, verdict: Verdict) = synchronized(CourierRuntime.lock) {
        check(open(context, p.packageId) == p) { "새 revision이 도착했습니다. 다시 확인하세요" }
        store(context).queue(CourierRuntime.routed(context), p, verdict)
        InboxRuntime.submitted(context, p.packageId, p.revision)
        CourierRuntime.kick(context)
        InboxRuntime.refresh(context)
    }
    private const val REJECT_DELAY_MS = 5_000L
    private fun pending(context: Context) = PendingRejects(File(AppStorage.exchangeDir(context), "reviews/pending-rejects.json"))
    fun pendingRejects(context: Context): List<PendingReject> = synchronized(CourierRuntime.lock) { pending(context).all() }
    private fun rejectWork(packageId: String) = "review-reject-$packageId"
    /** Persists the pending reject and enqueues a job that sends it, so it fires without any screen showing. */
    fun scheduleReject(context: Context, p: ReviewPackage, nowMs: Long = System.currentTimeMillis()): PendingReject =
        synchronized(CourierRuntime.lock) {
            PendingReject(p.packageId, p.revision, nowMs + REJECT_DELAY_MS).also { pending(context).schedule(it) }
        }.also {
            WorkManager.getInstance(context).enqueueUniqueWork(rejectWork(p.packageId), ExistingWorkPolicy.REPLACE,
                OneTimeWorkRequestBuilder<RejectWorker>()
                    .setInitialDelay((it.fireAt - System.currentTimeMillis()).coerceAtLeast(0), TimeUnit.MILLISECONDS).build())
            InboxRuntime.refresh(context)
        }
    /** Undo; returns false (a no-op) once the undo window has passed, because the reject is then being sent. */
    fun cancelReject(context: Context, packageId: String, nowMs: Long = System.currentTimeMillis()): Boolean {
        val cancelled = synchronized(CourierRuntime.lock) {
            val entry = pending(context).all().firstOrNull { it.packageId == packageId }
            entry != null && entry.fireAt > nowMs && pending(context).cancel(packageId)
        }
        if (cancelled) WorkManager.getInstance(context).cancelUniqueWork(rejectWork(packageId))
        InboxRuntime.refresh(context)
        return cancelled
    }
    /**
     * Sends due rejects. An entry is removed only once sent or obsolete (revision moved on, package unreadable);
     * a failed send keeps it for the next flush.
     */
    fun flushDueRejects(context: Context, nowMs: Long = System.currentTimeMillis()): List<String> {
        val sent = synchronized(CourierRuntime.lock) {
            val queue = pending(context)
            queue.due(nowMs).mapNotNull { entry ->
                val p = runCatching { open(context, entry.packageId) }.getOrNull()
                if (p == null || p.revision != entry.revision) {
                    queue.remove(entry.packageId, entry.revision)
                    return@mapNotNull null
                }
                if (runCatching { submit(context, p, ReviewDraft.reject(p)) }.isFailure) return@mapNotNull null
                queue.remove(entry.packageId, entry.revision)
                entry.packageId
            }
        }
        InboxRuntime.refresh(context)
        return sent
    }
    fun synchronize(context: Context, folder: CourierFolder): Boolean = synchronized(CourierRuntime.lock) {
        val store = store(context)
        InboxRuntime.reviews(context).forEach { store.observeRevision(it.packageId, it.revision) }
        store.synchronize(folder).also { InboxRuntime.refresh(context) }
    }
    /** Final answers the server gave over HTTP (applied, stale, conflict) become the record's status. */
    fun applyOutcomes(context: Context) = synchronized(CourierRuntime.lock) {
        val state = io.github.junhcha.weaver.transport.TransportRuntime.state(context)
        val outcomes = state.load().verdictOutcomes
        if (outcomes.isEmpty()) return@synchronized
        val store = store(context)
        outcomes.forEach { (name, outcome) ->
            val match = io.github.junhcha.weaver.courier.CourierRules.revision.matchEntire(name) ?: return@forEach
            store.mark(match.groupValues[1], match.groupValues[2].toLong(), when (outcome) {
                "APPLIED" -> ReviewStatus.APPLIED; "STALE" -> ReviewStatus.STALE
                "CONFLICT" -> ReviewStatus.CONFLICT; else -> ReviewStatus.ERROR
            })
        }
        state.update { it.copy(verdictOutcomes = it.verdictOutcomes - outcomes.keys) }
        InboxRuntime.refresh(context)
    }

    /** Unsent verdicts follow their feature to the new route; sent ones stay with the route that carried them. */
    fun retarget(context: Context, from: String, to: String) = synchronized(CourierRuntime.lock) { store(context).retarget(from, to) }

    fun retry(context: Context) { CourierRuntime.kick(context) }
}

/** Sends due rejects in the background; entries whose send failed are retried. */
class RejectWorker(context: Context, params: WorkerParameters) : Worker(context, params) {
    override fun doWork(): Result {
        ReviewRuntime.flushDueRejects(applicationContext)
        val now = System.currentTimeMillis()
        return if (ReviewRuntime.pendingRejects(applicationContext).any { it.fireAt <= now }) Result.retry() else Result.success()
    }
}
