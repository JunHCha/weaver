package io.github.junhcha.weaver.phone

import android.util.AtomicFile
import io.github.junhcha.weaver.schema.TaskItem
import java.io.File
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long

/**
 * Tasks the person checked on this phone, kept as `{threadId: checkedAtMs}` (ADR-043's local view state). A checked task
 * stays out of the list until the server stops listing it ([prune]); the completion itself is a request edit, not stored here.
 */
class DoneStore(file: File) {
    private val atomic = AtomicFile(file)
    private val lock = Any()
    private val checked = LinkedHashMap<String, Long>().apply { putAll(read()) }
    private val state = MutableStateFlow(checked.keys.toSet())

    /** The hidden thread ids, for screens that filter by themselves. */
    val ids: StateFlow<Set<String>> = state.asStateFlow()

    fun hide(id: String, now: Long) = synchronized(lock) { checked[id] = now; save() }

    fun unhide(id: String) = synchronized(lock) { if (checked.remove(id) != null) save() }

    fun visible(tasks: List<TaskItem>): List<TaskItem> = ids.value.let { hidden -> tasks.filter { it.threadId !in hidden } }

    /** Forgets ids the server no longer lists: the completion (or a recompile) reached the server's own list. */
    fun prune(serverIds: Set<String>) = synchronized(lock) { if (checked.keys.retainAll(serverIds)) save() }

    /**
     * Brings back tasks whose completion ended in the journal as refused or failed (a queued offline check that the server
     * rejected later). An already resolved one stays done, and so does one with another request still on its way.
     */
    fun reconcile(operations: List<PendingRequest>) = synchronized(lock) {
        val completes = operations.filter { it.path == "/requests/complete" }
        fun PendingRequest.thread() = (body["thread_id"] as? JsonPrimitive)?.content
        val live = completes.filter { it.state !in setOf("rejected", "failed") }.mapNotNull { it.thread() }.toSet()
        val revive = completes.filter { it.state == "failed" || (it.state == "rejected" && it.code != "request_already_resolved") }
            .mapNotNull { it.thread() }.filter { it !in live }
        var changed = false
        revive.forEach { if (checked.remove(it) != null) changed = true }
        if (changed) save()
    }

    private fun save() {
        state.value = checked.keys.toSet()
        val stream = atomic.startWrite()
        try {
            stream.write(JsonObject(checked.mapValues { JsonPrimitive(it.value) }).toString().toByteArray())
            atomic.finishWrite(stream)
        } catch (error: java.io.IOException) {
            atomic.failWrite(stream) // the hidden set still holds in memory for this run
        }
    }

    private fun read(): Map<String, Long> = runCatching {
        Json.parseToJsonElement(atomic.openRead().use { it.readBytes().decodeToString() }).jsonObject
            .mapValues { it.value.jsonPrimitive.long }
    }.getOrDefault(emptyMap())
}
