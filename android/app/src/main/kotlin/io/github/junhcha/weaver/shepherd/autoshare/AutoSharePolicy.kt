package io.github.junhcha.weaver.shepherd.autoshare

/** Device state sampled right before a job starts. */
data class RunEnvironment(val screenOn: Boolean, val unlocked: Boolean, val callIdle: Boolean)

enum class Blocker { SCREEN_OFF, LOCKED, IN_CALL }

enum class AutoShareOutcome { SUCCESS, FAILED, GAVE_UP }

/** Last recorded attempt for a call. Holds no script text, screen text or contact names. */
data class AutoShareAttempt(
    val callId: String,
    val attempts: Int,
    val lastStep: String?,
    val error: String?,
    val outcome: AutoShareOutcome,
    val at: Long,
)

object AutoSharePolicy {
    const val MAX_ATTEMPTS = 3
    /** A failed call is not retried before this delay, so a busy user is not interrupted repeatedly. */
    const val RETRY_DELAY_MS = 5 * 60_000L
    /** When a run is blocked by lock, screen or a call, look again after this delay. */
    const val BLOCKED_RETRY_MS = 5 * 60_000L
    private const val MIN_TRANSCRIBE_MS = 60_000L
    private const val MAX_TRANSCRIBE_MS = 20 * 60_000L
    /** Call recordings are roughly 8 KB per second; this overestimates duration for higher bitrates. */
    private const val BYTES_PER_SECOND = 8_000L

    fun blocker(env: RunEnvironment): Blocker? = when {
        !env.screenOn -> Blocker.SCREEN_OFF
        !env.unlocked -> Blocker.LOCKED
        !env.callIdle -> Blocker.IN_CALL
        else -> null
    }

    /** 60 s plus 1.5x the estimated recording length, capped at 20 minutes. */
    fun transcribeTimeoutMs(sizeBytes: Long): Long {
        val seconds = (sizeBytes.coerceAtLeast(0) / BYTES_PER_SECOND)
        return (MIN_TRANSCRIBE_MS + seconds * 1_500).coerceIn(MIN_TRANSCRIBE_MS, MAX_TRANSCRIBE_MS)
    }

    fun outcomeAfterFailure(attemptsIncludingThis: Int): AutoShareOutcome =
        if (attemptsIncludingThis >= MAX_ATTEMPTS) AutoShareOutcome.GAVE_UP else AutoShareOutcome.FAILED

    fun retryEligible(attempt: AutoShareAttempt?, now: Long): Boolean = when (attempt?.outcome) {
        null -> true
        AutoShareOutcome.FAILED -> now - attempt.at >= RETRY_DELAY_MS
        else -> false
    }
}
