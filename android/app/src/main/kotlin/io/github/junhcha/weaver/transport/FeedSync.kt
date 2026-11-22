package io.github.junhcha.weaver.transport

import io.github.junhcha.weaver.courier.CourierClient
import io.github.junhcha.weaver.pipeline.PipelineCache
import io.github.junhcha.weaver.schema.PipelineItem
import java.io.File
import java.io.FileOutputStream
import java.util.UUID
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject

/**
 * What the feed client remembers per server: how far it applied `/changes` ([cursor], cut for [kinds]) and the last
 * cursor the server confirmed ([checkpointed]). Separate from the Fabric `after_seq`, the person's confirmed seq and the
 * Action cursor: none of those is ever derived from or written into this.
 */
@Serializable
data class FeedData(
    val cursor: String? = null,
    /** The kinds [cursor] was cut for. A cursor is only valid for the set it was made with. */
    val kinds: String = "",
    val checkpointed: String? = null,
    /** Fixed code of the entry the feed stopped at, until a run completes. */
    val stopped: String? = null,
)

class FeedStore(private val file: File) {
    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }

    fun load(): FeedData = try { json.decodeFromString(file.readText()) } catch (_: Exception) { FeedData() }

    fun update(change: (FeedData) -> FeedData): FeedData {
        val next = change(load())
        file.parentFile?.mkdirs()
        val temp = File(file.parentFile, ".${UUID.randomUUID()}.tmp")
        try {
            FileOutputStream(temp).use { it.write(json.encodeToString(next).toByteArray()); it.fd.sync() }
            check(temp.renameTo(file)) { "feed_state_write_failed" }
        } finally { temp.delete() }
        return next
    }

    /** Forget the position: the next run takes a new snapshot. The cached bodies stay. */
    fun reset() { update { FeedData() } }
}

enum class FeedOutcome {
    /** Every entry up to the end of the feed was applied and checkpointed locally. */
    COMPLETE,
    /** An entry could not be applied (unknown kind, a refresh that failed). The cursor stops before it. */
    STOPPED,
}

/**
 * Pulls `/changes` for the kinds this phone consumes over HTTP and applies them. The feed is only a hint log: Alert,
 * review and action entries make the matching list refresh run ([refresh]), pipeline entries update the
 * [PipelineCache] (ETag/revision aware, tombstone removes). Rules this class keeps:
 *
 * - The cursor moves only after what its entries refer to is durable, and never past an entry that was not applied:
 *   an unknown kind or a failed refresh stops it right before that entry (an unreadable cursor shape stops it for the
 *   whole page). Earlier entries are re-delivered, which is safe because every application is idempotent.
 * - A different kinds set, a 410 or a changed epoch discard the cursor; the next snapshot replaces the pipeline list
 *   as a whole. Nothing is deleted because a kind left the feed (route change), because a list was filtered or
 *   because a request failed: only a tombstone or a complete list removes a cached item.
 * - Alert tombstones never touch the local body, read or closed state.
 * - The checkpoint PUT follows the local commit and is retried on the next run; its failure does not undo anything.
 */
class FeedSync(
    private val api: MobileApi,
    private val store: FeedStore,
    private val cache: PipelineCache,
    /** The kinds this phone consumes over HTTP right now (route-filtered), each one a [SUPPORTED_FEED_KINDS] member. */
    private val requested: Set<String>,
    /** Refreshes the full list of a kind (`review`, `action`, `alert`); false when it did not fully succeed. */
    private val refresh: (kind: String) -> Boolean,
    /** Runs one pipeline fetch with the exchange's error classification; false when it failed (it may throw to abort). */
    private val guard: (key: String, block: () -> Unit) -> Boolean,
    private val onInvalid: (key: String, code: String) -> Unit = { _, _ -> },
    private val now: () -> Long = System::currentTimeMillis,
) {
    private class Restart : RuntimeException()

    fun run(): FeedOutcome {
        var restarts = 0
        while (true) {
            val outcome = try { once() } catch (error: ApiException) {
                // 410 cursor_expired: the position (or the pipeline list under it) is gone. Take a new snapshot.
                if (((error.status == 410 && error.code == "cursor_expired") || (error.status == 422 && error.code == "invalid_cursor")) && restarts++ < 1) { store.reset(); continue }
                throw error
            } catch (_: Restart) { if (restarts++ < 1) { store.reset(); continue } else throw ApiException(410, "cursor_expired", false) }
            flushCheckpoint()
            return outcome
        }
    }

    private fun once(): FeedOutcome {
        val kindsKey = requested.sorted().joinToString(",")
        var data = store.load()
        if (data.kinds != kindsKey) data = store.update { FeedData(kinds = kindsKey) }
        var cursor = data.cursor
        // The first page of a snapshot also refreshes every list kind and replaces the pipeline list.
        val pending = linkedMapOf<String, Long>()
        if (cursor == null) {
            if ("pipeline" in requested && !replacePipeline()) return stop("feed_pipeline_list_failed")
            listOf("review", "action", "alert").filter { it in requested }.forEach { pending[it] = 0L }
        }
        var limit = LIMIT
        var pages = 0
        while (true) {
            val page = try { api.changes(cursor, requested, limit) } catch (error: ApiException) {
                if (error.code == "response_too_large" && limit > MIN_LIMIT) { limit = maxOf(MIN_LIMIT, limit / 2); continue }
                throw error
            }
            if (cursor != null && FeedCursor.epoch(cursor)?.let { it != page.feedEpoch } == true) throw Restart()
            check(++pages <= MAX_PAGES) { "too_many_pages" }
            var stopSeq: Long? = null
            var stopCode: String? = null
            val appliedSeqs = mutableListOf<Long>()
            for (entry in page.items) {
                val code = apply(entry, pending)
                if (code != null) { stopSeq = entry.seq; stopCode = code; break }
                appliedSeqs += entry.seq
            }
            // Refresh the list kinds this page (or the snapshot) touched; a failed kind holds the cursor before its
            // first entry, and the other kinds still ran.
            for (kind in pending.keys.toList()) {
                val first = pending.getValue(kind)
                pending.remove(kind)
                if (refresh(kind)) continue
                val current = stopSeq
                if (current == null || first < current) { stopSeq = first; stopCode = "feed_refresh_failed" }
            }
            val stoppedAt = stopSeq
            // Stopping at an entry keeps everything before it: the cursor goes back to the last applied entry below it.
            val applied = if (stoppedAt == null) null else appliedSeqs.lastOrNull { it < stoppedAt }
            val commit = when {
                stoppedAt == null -> page.nextCursor
                applied != null -> FeedCursor.upTo(page.nextCursor, applied)
                else -> null // nothing of this page was applied before the stop: keep the old cursor
            }
            if (commit != null && commit != cursor) { store.update { it.copy(cursor = commit, kinds = kindsKey, stopped = null) }; cursor = commit }
            if (stoppedAt != null) return stop(stopCode ?: "feed_stopped")
            if (!page.hasMore) { store.update { it.copy(stopped = null) }; return FeedOutcome.COMPLETE }
        }
    }

    private fun stop(code: String): FeedOutcome { store.update { it.copy(stopped = code) }; return FeedOutcome.STOPPED }

    /** Null when [entry] was applied (or deliberately ignored), otherwise the fixed code that stops the cursor here. */
    private fun apply(entry: FeedEntry, pending: MutableMap<String, Long>): String? {
        if (entry.kind !in SUPPORTED_FEED_KINDS) return "feed_kind_unsupported"
        // A supported kind this phone did not ask for (its route is files): never applied, so it cannot double-process.
        if (entry.kind !in requested) return null
        return when (entry.kind) {
            "review", "action" -> { pending.putIfAbsent(entry.kind, entry.seq); null }
            // A tombstone means the server dropped an expired body; the phone's own copy and read/closed state stay.
            "alert" -> { if (!entry.removed) pending.putIfAbsent("alert", entry.seq); null }
            "pipeline" -> pipeline(entry)
            else -> null // fabric/wiki are hints for the read model, which fetches on its own
        }
    }

    private fun pipeline(entry: FeedEntry): String? {
        val id = entry.id
        if (!PipelineCache.ID.matches(id)) return null.also { onInvalid("pipeline:invalid", "invalid_pipeline_id") }
        if (entry.removed) { cache.remove(id, now()); return null }
        val cached = cache.cached(id)
        val hintEtag = (entry.body as? JsonObject)?.get("etag")?.let { (it as? kotlinx.serialization.json.JsonPrimitive)?.content }
        val needed = cached == null ||
            (entry.revision != null && entry.revision > cached.revision) ||
            (hintEtag != null && cached.etag != null && hintEtag != cached.etag)
        if (!needed) return null
        cache.markApplying(id)
        return if (guard("pipeline:$id") { fetch(id, cached?.etag) }) null else "feed_pipeline_failed"
    }

    private fun fetch(id: String, etag: String?) {
        val reply = try { api.readPipelineItem(id, etag) } catch (error: ApiException) {
            // Gone already: the tombstone follows. Anything else is for the caller's classification.
            if (error.status == 404 && error.code == "pipeline_not_found") { cache.settled(id); return }
            throw error
        }
        if (reply == null) { cache.settled(id); return }
        val raw = try { feedJson.parseToJsonElement(reply.body.decodeToString()) as? JsonObject } catch (_: Exception) { null }
        // One bad body is isolated: it is reported and skipped so it cannot hold the feed (or the other items) back.
        if (raw == null || !cache.put(id, raw, reply.etag, now())) { onInvalid("pipeline:$id", "invalid_pipeline_item"); cache.settled(id) }
    }

    /** Full list into the cache. False when it failed for a reason the caller classified; 410 is thrown for a restart. */
    private fun replacePipeline(): Boolean {
        var ok = false
        var expired = false
        val success = guard("feed:pipeline-list") {
            val rows = try { Paging.all(HttpsMobileApi.PAGE, api::readPipeline) } catch (error: ApiException) {
                // The list changed under its cursor: the whole run starts over (outside the classified call).
                if (error.status == 410 && error.code == "cursor_expired") { expired = true; null } else throw error
            }
            if (rows != null) {
                val listed = mutableListOf<Triple<String, JsonObject, String?>>()
                val unreadable = mutableSetOf<String>()
                rows.forEach { element ->
                    val raw = element as? JsonObject ?: return@forEach
                    val id = PipelineCache.idOf(raw) ?: return@forEach
                    val valid = runCatching { CourierClient.decoded<PipelineItem>(raw.toString()) }.getOrNull()?.takeIf { it.sourceId == id }
                    if (valid == null) { unreadable += id; onInvalid("pipeline:$id", "invalid_pipeline_item") } else listed += Triple(id, raw, null)
                }
                cache.replaceAll(listed, unreadable, now())
                ok = true
            }
        }
        if (expired) throw Restart()
        return success && ok
    }

    private fun flushCheckpoint() {
        val data = store.load()
        val cursor = data.cursor ?: return
        if (cursor == data.checkpointed) return
        try {
            api.putCheckpoint(cursor)
            store.update { if (it.cursor == cursor) it.copy(checkpointed = cursor) else it }
        } catch (error: ApiException) {
            // The server no longer knows the position: start over. Auth problems are the exchange's to handle.
            if (error.status == 410) store.reset()
            else if (error.status == 401 || error.code == "node_mismatch") throw error
            // otherwise the PUT is simply retried by the next run; nothing local depends on it
        } catch (_: UnreachableException) { /* retried by the next run */
        } catch (_: InvalidResponseException) { /* retried by the next run */ }
    }

    companion object {
        const val LIMIT = 100
        const val MIN_LIMIT = 5
        const val MAX_PAGES = 5000
    }
}
