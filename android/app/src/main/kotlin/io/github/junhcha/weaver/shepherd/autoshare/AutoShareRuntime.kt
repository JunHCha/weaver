package io.github.junhcha.weaver.shepherd.autoshare

import android.app.KeyguardManager
import android.content.ComponentName
import android.content.Context
import android.media.AudioManager
import android.os.PowerManager
import android.provider.Settings
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import io.github.junhcha.weaver.shepherd.PendingCall
import io.github.junhcha.weaver.shepherd.ShepherdDatabase
import io.github.junhcha.weaver.shepherd.ShepherdRuntime
import io.github.junhcha.weaver.shepherd.ShepherdWorker
import io.github.junhcha.weaver.shepherd.SharedCallSelection
import java.util.concurrent.TimeUnit

enum class StartResult { DISABLED, SERVICE_OFF, BUSY, BLOCKED, NO_CANDIDATE, NOT_STARTED, STARTED }

/** What the status screen shows. */
data class AutoShareStatus(val enabled: Boolean, val serviceOn: Boolean, val last: AutoShareAttempt?)

/** Queue, run conditions and bookkeeping around [WeaverAutoShareService]. Default is OFF. */
object AutoShareRuntime {
    private const val KEY_ENABLED = "auto_share_enabled"
    private const val RETRY_WORK = "shepherd-autoshare-retry"

    class ActiveJob(val job: AutoShareJob) { @Volatile var submitted = false }

    @Volatile var active: ActiveJob? = null
        internal set
    /** Hands the job to the accessibility service; false when the service is not connected or busy. */
    internal var starter: (Context, ActiveJob) -> Boolean = { _, job -> WeaverAutoShareService.begin(job) }
    internal var environment: (Context) -> RunEnvironment = ::readEnvironment
    internal var serviceEnabled: (Context) -> Boolean = ::isServiceEnabledInSettings

    fun isEnabled(context: Context): Boolean = synchronized(ShepherdRuntime.lock) {
        ShepherdDatabase(context).use { it.setting(KEY_ENABLED) == "true" }
    }

    fun setEnabled(context: Context, enabled: Boolean) {
        synchronized(ShepherdRuntime.lock) {
            ShepherdDatabase(context).use {
                it.setting(KEY_ENABLED, enabled.toString())
                if (enabled) it.resetGaveUp()
            }
        }
        // Re-evaluate the manual-share notification, which auto share suppresses while it is trying.
        ShepherdRuntime.kick(context)
    }

    fun status(context: Context): AutoShareStatus = synchronized(ShepherdRuntime.lock) {
        ShepherdDatabase(context).use {
            AutoShareStatus(it.setting(KEY_ENABLED) == "true", serviceEnabled(context), it.lastAutoShare())
        }
    }

    /**
     * Calls the manual notification should not mention: those already shared automatically, and
     * (while auto share is on and the service is enabled) those auto share is still working on.
     */
    internal fun silenced(context: Context, db: ShepherdDatabase, unshared: List<String>): Set<String> {
        val shared = db.autoShareIds(AutoShareOutcome.SUCCESS)
        if (db.setting(KEY_ENABLED) != "true" || !serviceEnabled(context)) return shared
        val gaveUp = db.autoShareIds(AutoShareOutcome.GAVE_UP)
        return shared + unshared.filter { it !in gaveUp }
    }

    /** Start the next eligible call if the device is ready. Call from an IO thread. */
    fun maybeStart(context: Context, now: Long = System.currentTimeMillis()): StartResult {
        val job = synchronized(ShepherdRuntime.lock) {
            if (active != null) return StartResult.BUSY
            ShepherdDatabase(context).use { db ->
                if (db.setting(KEY_ENABLED) != "true") return StartResult.DISABLED
                if (!serviceEnabled(context)) return StartResult.SERVICE_OFF
                val candidate = db.autoShareCandidates(now).firstOrNull() ?: return StartResult.NO_CANDIDATE
                if (AutoSharePolicy.blocker(environment(context)) != null) {
                    scheduleRetry(context, AutoSharePolicy.BLOCKED_RETRY_MS, ExistingWorkPolicy.KEEP)
                    return StartResult.BLOCKED
                }
                ActiveJob(candidate).also { active = it }
            }
        }
        if (!starter(context, job)) {
            active = null
            return StartResult.NOT_STARTED
        }
        return StartResult.STARTED
    }

    /** Called by the service exactly once per job, on any thread. */
    fun onFinished(context: Context, job: AutoShareJob, progress: Progress, now: Long = System.currentTimeMillis()) {
        active = null
        synchronized(ShepherdRuntime.lock) {
            ShepherdDatabase(context).use { db ->
                when (progress) {
                    is Progress.Succeeded -> db.recordAutoShareSuccess(job.callId, now)
                    is Progress.Failed -> {
                        val outcome = db.recordAutoShareFailure(job.callId, progress.step, progress.error, now)
                        if (outcome == AutoShareOutcome.FAILED) {
                            scheduleRetry(context, AutoSharePolicy.RETRY_DELAY_MS, ExistingWorkPolicy.REPLACE)
                        }
                    }
                    Progress.Working -> Unit
                }
            }
        }
        // Success silences the manual notice; giving up releases it. Both need a fresh notification pass.
        ShepherdRuntime.kick(context)
    }

    /** ShareActivity asks which call to submit unattended; only the running job's own file qualifies. */
    fun autoSubmitCallId(displayName: String?, pending: List<PendingCall>): String? {
        val running = active ?: return null
        val stem = SharedCallSelection.stem(displayName) ?: return null
        if (nfc(stem) != nfc(running.job.stem)) return null
        return running.job.callId.takeIf { id -> pending.any { it.id == id } }
    }

    fun onSubmitted(callId: String) {
        active?.takeIf { it.job.callId == callId }?.submitted = true
    }

    internal fun scheduleRetry(context: Context, delayMs: Long, policy: ExistingWorkPolicy) {
        WorkManager.getInstance(context).enqueueUniqueWork(RETRY_WORK, policy,
            OneTimeWorkRequestBuilder<ShepherdWorker>().setInitialDelay(delayMs, TimeUnit.MILLISECONDS).build())
    }

    internal fun readEnvironment(context: Context): RunEnvironment {
        val power = context.getSystemService(PowerManager::class.java)
        val keyguard = context.getSystemService(KeyguardManager::class.java)
        val mode = context.getSystemService(AudioManager::class.java).mode
        // AudioManager avoids the READ_PHONE_STATE permission a telephony call-state check would need.
        val idle = mode != AudioManager.MODE_IN_CALL && mode != AudioManager.MODE_RINGTONE &&
            mode != AudioManager.MODE_IN_COMMUNICATION && mode != AudioManager.MODE_CALL_SCREENING
        return RunEnvironment(power.isInteractive, !keyguard.isKeyguardLocked, idle)
    }

    internal fun isServiceEnabledInSettings(context: Context): Boolean {
        val component = ComponentName(context, WeaverAutoShareService::class.java)
        val enabled = Settings.Secure.getString(context.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES)
        return enabled.orEmpty().split(':').any { ComponentName.unflattenFromString(it) == component }
    }
}
