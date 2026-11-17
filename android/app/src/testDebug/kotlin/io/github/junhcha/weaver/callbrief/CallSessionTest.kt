package io.github.junhcha.weaver.callbrief

import org.junit.Assert.*
import org.junit.Test

class CallSessionTest {
    @Test fun incomingCallCoalescesDuplicateBroadcastsAndEndsOnce() {
        val session = CallSession()
        assertFalse(session.accept("IDLE", null))
        assertTrue(session.accept("RINGING", null))
        assertTrue(session.accept("RINGING", "5550100"))
        assertFalse(session.accept("RINGING", null))
        assertEquals("5550100", session.number)
        assertTrue(session.accept("OFFHOOK", null))
        assertFalse(session.accept("OFFHOOK", "5550100"))
        assertTrue(session.accept("IDLE", null))
        assertFalse(session.accept("IDLE", "5550100"))
    }

    @Test fun outgoingCallDoesNotInheritPreviousCaller() {
        val session = CallSession()
        assertTrue(session.accept("RINGING", "5550100"))
        assertTrue(session.accept("IDLE", null))
        assertTrue(session.accept("OFFHOOK", null))
        assertNull(session.number)
        assertFalse(session.accept("invalid", "5550199"))
        assertNull(session.number)
        assertTrue(session.accept("IDLE", null))
    }
}
