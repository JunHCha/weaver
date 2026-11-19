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

/** Device-local handling and durable write journal. All network methods run on Dispatchers.IO. */
class NowController(val server: String, private val api: () -> MobileApi?, private val store: NowStore,
                    private val clock: () -> Instant = Instant::now) {
    @Volatile var snapshot: NowSnapshot? = null; private set
    @Volatile var supported = false; private set
    @Volatile var canEdit = false; private set
    @Volatile var online = false; private set
    @Volatile var problem: String? = null; private set

    private val refreshLock = Mutex()
    private val submitLock = Mutex()

    suspend fun refresh() = refreshLock.withLock {
        withContext(Dispatchers.IO) {
            online = false
            try {
                val client = api() ?: error("offline")
                val caps = client.capabilities()
                require(caps["server_id"]?.jsonPrimitive?.content == server)
                supported = (caps["features"] as? JsonObject)?.get("thread_handling") == JsonPrimitive(true)
                canEdit = (caps["device"] as? JsonObject)?.get("scopes")?.jsonArray?.any { it.jsonPrimitive.content == "edit" } == true
                if (!supported) { snapshot = null; problem = null; return@withContext }
                val cached = store.snapshot(server)?.let(NowSnapshot::parse)
                var epoch = snapshot?.epoch ?: cached?.epoch
                if (epoch == null) epoch = NowSnapshot.parse(client.now()).epoch
                fun read(e: String) = client.now(store.ack(server, e), e)
                val raw = try { read(epoch) } catch (error: ApiException) {
                    if (error.code != "epoch_mismatch") throw error
                    epoch = NowSnapshot.parse(client.now()).epoch
                    read(epoch)
                }
                val fresh = NowSnapshot.parse(raw) // never skip an unknown or malformed row
                store.quarantine(server, fresh.epoch)
                fresh.threads.forEach { store.suppressed(server, fresh.epoch, it, clock()) }
                store.saveSnapshot(server, fresh.epoch, raw)
                snapshot = fresh
                online = true
                problem = null
                if (canEdit && replay(client, fresh.epoch)) {
                    val updated = read(fresh.epoch)
                    snapshot = NowSnapshot.parse(updated)
                    store.saveSnapshot(server, fresh.epoch, updated)
                }
            } catch (error: CancellationException) { throw error
            } catch (error: Exception) {
                online = false
                problem = if (error is ApiException) error.code else "now_unavailable"
                if (snapshot == null) snapshot = runCatching { store.snapshot(server)?.let(NowSnapshot::parse) }.getOrNull()
                // A persisted now snapshot proves support even when capabilities cannot be fetched offline.
                if (snapshot != null) supported = true
            }
        }
    }

    private fun replay(client: MobileApi, epoch: String): Boolean {
        var changed = false
        for (pending in store.operations(server).filter { it.state == "pending" && it.epoch == epoch }) {
            try {
                val response = client.edit(pending.path, pending.body)
                val state = response["state"]?.jsonPrimitive?.content ?: error("invalid_operation")
                when (state) {
                    "applied" -> {
                        val result = response["result"] as? JsonObject ?: error("invalid_result")
                        store.finish(pending.clientId, "applied", result)
                        changed = true
                    }
                    "rejected", "failed" -> store.finish(pending.clientId, state, code = response["code"]?.jsonPrimitive?.contentOrNull)
                    "queued" -> Unit
                    else -> error("invalid_operation")
                }
            } catch (error: ApiException) {
                if (error.status in listOf(400, 403, 404, 409, 422)) {
                    store.finish(pending.clientId, "rejected", code = error.code)
                    problem = error.code
                } else throw error
            }
        }
        return changed
    }

    fun acknowledge(): Boolean {
        val current = snapshot ?: return false
        if (!supported || !online) return false
        store.acknowledge(server, current.epoch, current.head)
        return true
    }

    fun visible(now: Instant = clock()): NowView? {
        val current = snapshot ?: return null
        val base = if (online) current.view else {
            val recategorized = nowView(PhoneSnapshot(current.threads, emptyList(), emptyList()), now, current.zone)
            val listed = (recategorized.overdue + recategorized.today).map { it.thread.id }.toSet()
            recategorized.copy(recent = current.threads.filter {
                it.id !in listed && maxOf(it.seq, it.revision) > store.ack(server, current.epoch)
            }.map { NowEntry(it) })
        }
        fun filter(items: List<NowEntry>) = items.filterNot { store.suppressed(server, current.epoch, it.thread, now) }
        return NowView(filter(base.overdue), filter(base.today), filter(base.recent).filter {
            maxOf(it.thread.seq, it.thread.revision) > store.ack(server, current.epoch)
        })
    }
    fun suppressed(now: Instant = clock()): List<PhoneThread> = snapshot?.let { current ->
        (current.threads + store.savedViews(server, current.epoch)).distinctBy { it.id }.filter { store.suppressed(server, current.epoch, it, now) }
    }.orEmpty()
    fun hide(thread: PhoneThread) { snapshot?.let { store.setView(server, it.epoch, thread, hidden = true) } }
    fun snooze(thread: PhoneThread, until: Instant) {
        require(until.isAfter(clock()))
        snapshot?.let { store.setView(server, it.epoch, thread, until) }
    }
    fun tomorrow(): Instant = clock().atZone(snapshot!!.zone).toLocalDate().plusDays(1).atTime(9, 0).atZone(snapshot!!.zone).toInstant()
    fun restore(thread: PhoneThread) { snapshot?.let { store.restore(server, it.epoch, thread.id) } }
    fun nextRefresh(now: Instant): Instant {
        val current = snapshot ?: return now.plusSeconds(60)
        val midnight = now.atZone(current.zone).toLocalDate().plusDays(1).atStartOfDay(current.zone).toInstant()
        val boundary = minOf(midnight, store.nextExpiry(server, current.epoch, now) ?: midnight)
        val retry = !online || store.operations(server).any { it.epoch == current.epoch && it.state == "pending" }
        return if (retry) minOf(boundary, now.plusSeconds(60)) else boundary
    }
    fun operations(): List<PendingRequest> = store.operations(server)

    suspend fun submit(thread: PhoneThread, action: String, content: String = "", deadline: LocalDate? = null,
                       editThreadId: String? = null) = submitLock.withLock { withContext(Dispatchers.IO) {
        check(supported && online && canEdit)
        require(action in setOf("complete", "deadline", "undo"))
        val current = snapshot ?: error("no_snapshot")
        check(store.operations(server).none {
            it.epoch == current.epoch && it.state == "pending" &&
                (it.body["thread_id"]?.jsonPrimitive?.contentOrNull == thread.id ||
                    (editThreadId != null && it.body["edit_thread_id"]?.jsonPrimitive?.contentOrNull == editThreadId))
        }) { "pending_request_exists" }
        val clientId = UUID.randomUUID().toString()
        val body = buildJsonObject {
            put("client_id", clientId); put("expected_revision", thread.revision)
            if (action == "undo") put("edit_thread_id", requireNotNull(editThreadId))
            else {
                require(content.isNotBlank() && content.length <= 2000)
                put("thread_id", thread.id); put("content", content)
                if (action == "deadline") put("deadline", requireNotNull(deadline).toString())
            }
        }
        store.enqueue(server, current.epoch, clientId, "/requests/$action", body)
        // Refresh the generation before replay, including on a retry after a lost response.
        refresh()
    } }
}
