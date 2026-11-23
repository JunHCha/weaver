package io.github.junhcha.weaver.review

import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.io.File

@RunWith(RobolectricTestRunner::class)
class PendingRejectsTest {
    @get:Rule val tmp = TemporaryFolder()
    private fun file() = File(tmp.root, "reviews/pending-rejects.json")
    private val a = PendingReject("A", 1, 1000)
    private val b = PendingReject("B", 1, 3000)

    @Test fun scheduleCancelDue() {
        val q = PendingRejects(file())
        q.schedule(a); q.schedule(b)
        assertEquals(listOf(a), q.due(2000))
        q.remove("A", 1)
        assertEquals(listOf(b), q.all())
        assertTrue(q.cancel("B"))
        assertTrue(q.all().isEmpty())
        assertFalse(q.cancel("B"))
    }

    @Test fun dueRejectSurvivesRestart() {
        PendingRejects(file()).schedule(a)
        assertEquals(listOf(a), PendingRejects(file()).due(1000))
    }

    @Test fun rescheduleSamePackageReplaces() {
        val q = PendingRejects(file())
        q.schedule(a); q.schedule(a.copy(revision = 2, fireAt = 5000))
        assertEquals(listOf(PendingReject("A", 2, 5000)), q.all())
    }

    @Test fun dueKeepsEntriesUntilRemoved() {
        val q = PendingRejects(file())
        q.schedule(a); q.schedule(b)
        assertEquals(listOf(a), q.due(2000))
        assertEquals(listOf(a, b), PendingRejects(file()).all())
        q.remove("A", 2)
        assertEquals(2, q.all().size)
        q.remove("A", 1)
        assertEquals(listOf(b), q.all())
    }
}
