package io.github.junhcha.weaver.shepherd.autoshare

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class AutoSharePolicyTest {
    @Test fun runConditionsAreCheckedInOrder() {
        assertNull(AutoSharePolicy.blocker(RunEnvironment(screenOn = true, unlocked = true, callIdle = true)))
        assertEquals(Blocker.SCREEN_OFF, AutoSharePolicy.blocker(RunEnvironment(false, true, true)))
        assertEquals(Blocker.LOCKED, AutoSharePolicy.blocker(RunEnvironment(true, false, true)))
        assertEquals(Blocker.IN_CALL, AutoSharePolicy.blocker(RunEnvironment(true, true, false)))
    }

    @Test fun transcriptionLimitScalesWithSizeAndIsCapped() {
        assertEquals(60_000, AutoSharePolicy.transcribeTimeoutMs(0))
        assertEquals(60_000 + 60 * 1_500, AutoSharePolicy.transcribeTimeoutMs(480_000))
        assertEquals(20 * 60_000L, AutoSharePolicy.transcribeTimeoutMs(Long.MAX_VALUE / 4))
    }

    @Test fun thirdFailureGivesUp() {
        assertEquals(AutoShareOutcome.FAILED, AutoSharePolicy.outcomeAfterFailure(1))
        assertEquals(AutoShareOutcome.FAILED, AutoSharePolicy.outcomeAfterFailure(2))
        assertEquals(AutoShareOutcome.GAVE_UP, AutoSharePolicy.outcomeAfterFailure(3))
    }

    @Test fun retryWaitsForTheDelayAndNeverAfterSuccessOrGivingUp() {
        fun attempt(outcome: AutoShareOutcome) = AutoShareAttempt("c", 1, null, null, outcome, 1_000)
        assertTrue(AutoSharePolicy.retryEligible(null, 0))
        assertFalse(AutoSharePolicy.retryEligible(attempt(AutoShareOutcome.FAILED), 1_000 + AutoSharePolicy.RETRY_DELAY_MS - 1))
        assertTrue(AutoSharePolicy.retryEligible(attempt(AutoShareOutcome.FAILED), 1_000 + AutoSharePolicy.RETRY_DELAY_MS))
        assertFalse(AutoSharePolicy.retryEligible(attempt(AutoShareOutcome.SUCCESS), Long.MAX_VALUE))
        assertFalse(AutoSharePolicy.retryEligible(attempt(AutoShareOutcome.GAVE_UP), Long.MAX_VALUE))
    }
}
