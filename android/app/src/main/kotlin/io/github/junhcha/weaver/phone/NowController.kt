package io.github.junhcha.weaver.phone

import io.github.junhcha.weaver.transport.ApiException
import io.github.junhcha.weaver.transport.MobileApi
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.util.UUID
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import android.database.sqlite.SQLiteException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.*

/** A single, fully parsed /read/now response. Its head never comes from the general thread feed. */
data class NowSnapshot(val head: Long, val epoch: String, val zone: ZoneId, val generatedAt: Instant,
                       val view: NowView) {
    val threads: List<PhoneThread> get() = (view.overdue + view.today + view.recent).map { it.thread }.distinctBy { it.id }
    companion object {
        fun parse(root: JsonObject): NowSnapshot {
            val head = root.getValue("head").jsonPrimitive.long.also { require(it >= 0) }
            val epoch = root.getValue("fabric_epoch").jsonPrimitive.content.also { require(it.isNotBlank()) }
            val zone = ZoneId.of(root.getValue("timezone").jsonPrimitive.content)
            val generated = Instant.parse(root.getValue("generated_at").jsonPrimitive.content)
            LocalDate.parse(root.getValue("today").jsonPrimitive.content)
            fun entries(key: String) = root.getValue(key).jsonArray.map { item ->
                val entry = item.jsonObject
                val raw = entry.getValue("thread").jsonObject
                val thread = HttpPhoneReadModel.thread(raw)
                require(thread.seq in 1..head && raw.getValue("revision").jsonPrimitive.long == thread.revision && thread.revision in thread.seq..head)
                require(raw.getValue("content").jsonPrimitive.isString)
                require(thread.open != null && thread.retracted != null && thread.presentation != null)
                raw.getValue("effective_deadline").let { if (it != JsonNull) LocalDate.parse(it.jsonPrimitive.content) }
                raw.getValue("effective_schedule").let { if (it != JsonNull) Instant.parse(it.jsonObject.getValue("start").jsonPrimitive.content) }
                val kind = when (entry.getValue("due_kind")) {
                    JsonNull -> null
                    JsonPrimitive("deadline") -> DueKind.DEADLINE
                    JsonPrimitive("schedule") -> DueKind.SCHEDULE
                    else -> error("invalid_due_kind")
                }
                NowEntry(thread, kind)
            }
            return NowSnapshot(head, epoch, zone, generated, NowView(entries("overdue"), entries("due_today"), entries("recent")))
        }
    }
}

/** One immutable publication; UI reads never open SQLite or clean up stored rows. */
data class NowUiState(
    val snapshot: NowSnapshot? = null,
    val supported: Boolean = false,
    val canEdit: Boolean = false,
    val online: Boolean = false,
    val problem: String? = null,
    val visible: NowView? = null,
    val suppressed: List<PhoneThread> = emptyList(),
    val operations: List<PendingRequest> = emptyList(),
    val nextRefreshAt: Instant? = null,
)

/** Device-local handling and durable write journal. Every store access runs on Dispatchers.IO. */
class NowController(val server: String, private val api: () -> MobileApi?, private val store: NowStore,
                    private val clock: () -> Instant = Instant::now) {
    private val mutableUiState = MutableStateFlow(NowUiState())
    val uiState: StateFlow<NowUiState> = mutableUiState.asStateFlow()
    val snapshot: NowSnapshot? get() = uiState.value.snapshot
    val supported: Boolean get() = uiState.value.supported
    val canEdit: Boolean get() = uiState.value.canEdit
    val online: Boolean get() = uiState.value.online
    val problem: String? get() = uiState.value.problem
    private val refreshLock = Mutex()
    private val submitLock = Mutex()

    suspend fun refresh() = refreshLock.withLock {
        withContext(Dispatchers.IO) {
            var next = uiState.value
            try {
                store.writableDatabase // open/create/migrate before exposing any UI data
                val client = api() ?: error("offline")
                val caps = client.capabilities()
                require(caps["server_id"]?.jsonPrimitive?.content == server)
                val supported = (caps["features"] as? JsonObject)?.get("thread_handling") == JsonPrimitive(true)
                val canEdit = (caps["device"] as? JsonObject)?.get("scopes")?.jsonArray?.any { it.jsonPrimitive.content == "edit" } == true
                next = next.copy(supported = supported, canEdit = canEdit)
                if (!supported) {
                    mutableUiState.value = NowUiState(canEdit = canEdit)
                    return@withContext
                }
                val cached = store.snapshot(server)?.let(NowSnapshot::parse)
                var epoch = next.snapshot?.epoch ?: cached?.epoch
                if (epoch == null) epoch = NowSnapshot.parse(client.now()).epoch
                fun read(e: String) = client.now(store.ack(server, e), e)
                val raw = try { read(epoch) } catch (error: ApiException) {
                    when (error.code) {
                        "epoch_mismatch" -> epoch = NowSnapshot.parse(client.now()).epoch
                        "checkpoint_out_of_range" -> store.resetAck(server, epoch)
                        else -> throw error
                    }
                    read(epoch)
                }
                val fresh = NowSnapshot.parse(raw)
                store.quarantine(server, fresh.epoch)
                store.saveSnapshot(server, fresh.epoch, raw)
                next = next.copy(snapshot = fresh, online = true, problem = null)
                val replayed = if (canEdit) replay(client, fresh.epoch) else ReplayResult(false, null)
                next = next.copy(problem = replayed.problem)
                if (replayed.changed) {
                    val updated = read(fresh.epoch)
                    val parsed = NowSnapshot.parse(updated)
                    store.saveSnapshot(server, parsed.epoch, updated)
                    next = next.copy(snapshot = parsed)
                }
            } catch (error: CancellationException) { throw error
            } catch (error: Exception) {
                val cached = next.snapshot ?: runCatching { store.snapshot(server)?.let(NowSnapshot::parse) }.getOrNull()
                next = next.copy(snapshot = cached, online = false, supported = next.supported || cached != null,
                    problem = if (error is ApiException) error.code else "now_unavailable")
            }
            publishSafely(next)
        }
    }

    private data class ReplayResult(val changed: Boolean, val problem: String?)

    private fun replay(client: MobileApi, epoch: String): ReplayResult {
        var changed = false
        var problem: String? = null
        for (pending in store.operations(server).filter { it.state == "pending" && it.epoch == epoch }) {
            try {
                val response = client.edit(pending.path, pending.body)
                when (response["state"]?.jsonPrimitive?.content ?: error("invalid_operation")) {
                    "applied" -> {
                        val result = response["result"] as? JsonObject ?: error("invalid_result")
                        require(result.getValue("revision").jsonPrimitive.long > 0)
                        store.finish(pending.clientId, "applied", result)
                        changed = true
                    }
                    "rejected", "failed" -> {
                        problem = response["code"]?.jsonPrimitive?.contentOrNull
                        store.finish(pending.clientId, response.getValue("state").jsonPrimitive.content, code = problem)
                    }
                    "queued" -> Unit
                    else -> error("invalid_operation")
                }
            } catch (error: ApiException) {
                if (error.status in listOf(400, 404, 409, 422)) {
                    store.finish(pending.clientId, "rejected", code = error.code)
                    problem = error.code
                } else throw error // 403 keeps the same pending key/body for a repaired token
            }
        }
        return ReplayResult(changed, problem)
    }

    /** Called only on IO, including cleanup of expired, revised and damaged local rows. */
    private fun publish(next: NowUiState) {
        val current = next.snapshot
        val now = clock()
        val operations = store.operations(server)
        if (current == null) {
            mutableUiState.value = next.copy(operations = operations, nextRefreshAt = if (next.problem == "response_too_large") null else now.plusSeconds(60))
            return
        }
        val ack = store.ack(server, current.epoch)
        val hidden = (current.threads + store.savedViews(server, current.epoch)).distinctBy { it.id }
            .filter { store.suppressed(server, current.epoch, it, now) }
        val hiddenIds = hidden.map { it.id }.toSet()
        val base = if (next.online) current.view else {
            val grouped = nowView(PhoneSnapshot(current.threads, emptyList(), emptyList()), now, current.zone)
            val listed = (grouped.overdue + grouped.today).map { it.thread.id }.toSet()
            grouped.copy(recent = current.threads.filter { it.id !in listed && maxOf(it.seq, it.revision) > ack }.map { NowEntry(it) })
        }
        fun filter(items: List<NowEntry>) = items.filterNot { it.thread.id in hiddenIds }
        val visible = NowView(filter(base.overdue), filter(base.today), filter(base.recent).filter { maxOf(it.thread.seq, it.thread.revision) > ack })
        val midnight = now.atZone(current.zone).toLocalDate().plusDays(1).atStartOfDay(current.zone).toInstant()
        val boundary = minOf(midnight, store.nextExpiry(server, current.epoch, now) ?: midnight)
        // Oversized unpaged responses require a server pagination follow-up; avoid automatic error loops.
        val retry = next.problem != "response_too_large" && (!next.online || operations.any { it.epoch == current.epoch && it.state == "pending" })
        mutableUiState.value = next.copy(visible = visible, suppressed = hidden, operations = operations,
            nextRefreshAt = if (retry) minOf(boundary, now.plusSeconds(60)) else boundary)
    }
    private fun publishSafely(next: NowUiState) {
        try { publish(next) }
        catch (_: SQLiteException) { mutableUiState.value = next.copy(problem = "local_storage_error") }
    }

    suspend fun refreshLocal() = refreshLock.withLock {
        withContext(Dispatchers.IO) { publishSafely(uiState.value) }
    }

    suspend fun acknowledge(renderedHead: Long, renderedEpoch: String): Boolean = localWrite {
        val current = snapshot ?: return@localWrite false
        if (!supported || !online || current.head != renderedHead || current.epoch != renderedEpoch) return@localWrite false
        store.acknowledge(server, renderedEpoch, renderedHead)
        true
    }

    // These compatibility accessors are pure immutable reads, safe during composition.
    fun visible(): NowView? = uiState.value.visible
    fun suppressed(): List<PhoneThread> = uiState.value.suppressed
    fun operations(): List<PendingRequest> = uiState.value.operations
    fun nextRefresh(now: Instant): Instant = uiState.value.nextRefreshAt ?: now.plusSeconds(60)

    private suspend fun localWrite(block: () -> Boolean): Boolean = refreshLock.withLock {
        withContext(Dispatchers.IO) {
            try {
                val success = block()
                publish(uiState.value)
                success
            } catch (_: SQLiteException) {
                mutableUiState.value = uiState.value.copy(problem = "local_storage_error")
                false
            }
        }
    }
    suspend fun hide(thread: PhoneThread): Boolean = localWrite {
        val current = snapshot ?: return@localWrite false
        store.setView(server, current.epoch, thread, hidden = true)
        true
    }
    suspend fun snooze(thread: PhoneThread, until: Instant): Boolean = localWrite {
        val current = snapshot ?: return@localWrite false
        if (!until.isAfter(clock())) return@localWrite false
        store.setView(server, current.epoch, thread, until)
        true
    }
    fun tomorrow(): Instant = clock().atZone(snapshot!!.zone).toLocalDate().plusDays(1).atTime(9, 0).atZone(snapshot!!.zone).toInstant()
    suspend fun restore(thread: PhoneThread): Boolean = localWrite {
        val current = snapshot ?: return@localWrite false
        store.restore(server, current.epoch, thread.id)
        true
    }

    /** Undo always guards the revision produced by this operation, never a later read projection. */
    suspend fun undo(operation: PendingRequest) {
        require(operation.state == "applied" && operation.epoch == snapshot?.epoch && operation.path != "/requests/undo")
        val result = requireNotNull(operation.result)
        send("undo", result.getValue("revision").jsonPrimitive.long, result.getValue("thread_id").jsonPrimitive.content,
            editThreadId = result.getValue("edit_thread_id").jsonPrimitive.content)
    }
    suspend fun submit(thread: PhoneThread, action: String, content: String = "", deadline: LocalDate? = null) {
        require(action in setOf("complete", "deadline"))
        send(action, thread.revision, thread.id, content, deadline)
    }
    private suspend fun send(action: String, revision: Long, threadId: String, content: String = "", deadline: LocalDate? = null,
                             editThreadId: String? = null) = submitLock.withLock {
        withContext(Dispatchers.IO) {
            check(supported && online && canEdit)
            val current = snapshot ?: error("no_snapshot")
            check(store.operations(server).none {
                it.epoch == current.epoch && it.state == "pending" &&
                    (it.body["thread_id"]?.jsonPrimitive?.contentOrNull == threadId ||
                        (editThreadId != null && it.body["edit_thread_id"]?.jsonPrimitive?.contentOrNull == editThreadId))
            }) { "pending_request_exists" }
            val clientId = UUID.randomUUID().toString()
            val body = buildJsonObject {
                put("client_id", clientId); put("expected_revision", revision)
                if (action == "undo") put("edit_thread_id", requireNotNull(editThreadId))
                else {
                    require(content.isNotBlank() && content.length <= 2000)
                    put("thread_id", threadId); put("content", content)
                    if (action == "deadline") put("deadline", requireNotNull(deadline).toString())
                }
            }
            store.enqueue(server, current.epoch, clientId, "/requests/$action", body)
            refresh()
        }
    }
}
