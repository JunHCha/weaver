package io.github.junhcha.weaver.phone

import io.github.junhcha.weaver.schema.TaskItem
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class DoneStoreTest {
    @get:Rule val tmp = TemporaryFolder()

    private fun task(id: String) = TaskItem(id, 1, "회의실 예약 $id", "b-$id", null, null, null, null, null, null)

    @Test fun hidePruneSurvivesRestart() {
        val file = File(tmp.root, "done.json")
        DoneStore(file).apply { hide("A", 1_000); hide("B", 2_000) }
        val restarted = DoneStore(file)
        assertEquals(listOf("C"), restarted.visible(listOf(task("A"), task("B"), task("C"))).map { it.threadId })
        restarted.prune(setOf("B", "C"))
        assertEquals(listOf("C"), DoneStore(file).visible(listOf(task("B"), task("C"))).map { it.threadId })
    }

    @Test fun unhideShowsTheTaskAgainAndSurvivesRestart() {
        val file = File(tmp.root, "done.json")
        val store = DoneStore(file)
        store.hide("A", 1_000)
        store.unhide("A")
        assertTrue(store.ids.value.isEmpty())
        assertEquals(listOf("A"), DoneStore(file).visible(listOf(task("A"))).map { it.threadId })
    }

    @Test fun damagedFileStartsEmpty() {
        val file = File(tmp.root, "done.json").apply { writeText("{broken") }
        assertEquals(listOf("A"), DoneStore(file).visible(listOf(task("A"))).map { it.threadId })
    }

    private fun op(state: String, thread: String, code: String? = null, path: String = "/requests/complete") =
        PendingRequest("e", "c-$state-$thread", path, kotlinx.serialization.json.buildJsonObject { put("thread_id", kotlinx.serialization.json.JsonPrimitive(thread)) }, state, null, code)

    @Test fun reconcileBringsBackRefusedOrFailedChecksOnly() {
        val file = File(tmp.root, "done.json")
        val store = DoneStore(file)
        listOf("refused", "failed", "resolved", "queued", "again", "applied", "other").forEach { store.hide(it, 1) }
        store.reconcile(listOf(
            op("rejected", "refused", "revision_conflict"), op("failed", "failed"),
            op("rejected", "resolved", "request_already_resolved"), op("pending", "queued"),
            op("rejected", "again", "revision_conflict"), op("pending", "again"), op("applied", "applied"),
            op("rejected", "other", "revision_conflict", path = "/requests/deadline"),
        ))
        assertEquals(setOf("resolved", "queued", "again", "applied", "other"), store.ids.value)
        assertEquals(store.ids.value, DoneStore(file).ids.value)
    }
}
