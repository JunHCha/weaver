package io.github.junhcha.weaver.pipeline

import android.util.AtomicFile
import io.github.junhcha.weaver.schema.PipelineItem
import io.github.junhcha.weaver.schema.WeaverCodec
import java.io.File
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/** What the cache knows about one item without handing out the whole body. */
data class CachedPipeline(val sourceId: String, val revision: Long, val etag: String?)

/**
 * What a screen needs to show next to the cached map: how many items there are, which ones the server announced but
 * the phone has not fetched yet (the "반영중" mark), and when the cache last matched the server.
 */
data class PipelineCacheStatus(val items: Int, val applying: Set<String>, val syncedAt: Long?)

@Serializable private data class CacheEntry(val etag: String? = null, val item: JsonObject)

@Serializable private data class CacheMeta(val applying: Set<String> = emptySet(), val syncedAt: Long? = null)

/**
 * Durable copy of the server's `/read/pipeline` list for one server (the HTTP source of the map; it is not the
 * Syncthing-file [PipelineStore]). Data layer only: nothing here draws anything, and every call is plain file IO for a
 * worker or an IO coroutine, never for composition.
 *
 * Rules: a body is written atomically before anything refers to it; an announced-but-missing item stays in
 * [PipelineCacheStatus.applying] until it is fetched; only a tombstone ([remove]) or a complete listing ([replaceAll])
 * removes an item; one unreadable file never hides the others.
 */
class PipelineCache(private val directory: File) {
    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }
    private val itemsDir get() = File(directory, "items")
    private val metaFile get() = File(directory, "meta.json")

    private fun file(sourceId: String): File {
        require(ID.matches(sourceId)) { "invalid_source_id" }
        return File(itemsDir, "$sourceId.json")
    }

    private fun write(target: File, bytes: ByteArray) {
        target.parentFile?.let { check(it.isDirectory || it.mkdirs()) { "cache_unavailable" } }
        val atomic = AtomicFile(target)
        val output = atomic.startWrite()
        try { output.write(bytes); atomic.finishWrite(output) } catch (error: Exception) { atomic.failWrite(output); throw error }
    }

    private fun entry(sourceId: String): Pair<CacheEntry, PipelineItem>? = runCatching {
        val target = file(sourceId)
        val entry = json.decodeFromString<CacheEntry>(AtomicFile(target).openRead().bufferedReader().use { it.readText() })
        entry to WeaverCodec.json.decodeFromString<PipelineItem>(entry.item.toString())
    }.getOrNull()

    private fun readMeta(): CacheMeta = runCatching {
        json.decodeFromString<CacheMeta>(AtomicFile(metaFile).openRead().bufferedReader().use { it.readText() })
    }.getOrDefault(CacheMeta())

    private fun updateMeta(change: (CacheMeta) -> CacheMeta) = write(metaFile, json.encodeToString(change(readMeta())).toByteArray())

    private fun ids(): List<String> = itemsDir.listFiles().orEmpty().map { it.name.removeSuffix(".bak") }
        .filter { it.endsWith(".json") }.map { it.removeSuffix(".json") }.filter { ID.matches(it) }.distinct().sorted()

    private fun decode(sourceId: String, raw: JsonObject): PipelineItem? = runCatching {
        WeaverCodec.json.decodeFromString<PipelineItem>(raw.toString()).takeIf { it.sourceId == sourceId }
    }.getOrNull()

    /** Decoded items in `source_id` order; a damaged file is skipped, never fatal. */
    fun items(): List<PipelineItem> = ids().mapNotNull { entry(it)?.second }

    fun cached(sourceId: String): CachedPipeline? =
        if (!ID.matches(sourceId)) null else entry(sourceId)?.let { (e, item) -> CachedPipeline(sourceId, item.revision, e.etag) }

    fun status(): PipelineCacheStatus = readMeta().let { PipelineCacheStatus(ids().size, it.applying, it.syncedAt) }

    /** The server announced this item and its body has not arrived yet. */
    fun markApplying(sourceId: String) {
        if (ID.matches(sourceId)) updateMeta { it.copy(applying = it.applying + sourceId) }
    }

    /**
     * Stores [raw] after validating it strictly as the PipelineItem [sourceId]. A lower revision than the cached one is
     * kept out (a stale response must not roll the map back). Returns false when the body is not valid; nothing changes.
     */
    fun put(sourceId: String, raw: JsonObject, etag: String?, now: Long): Boolean {
        if (!ID.matches(sourceId)) return false
        val item = decode(sourceId, raw) ?: return false
        if ((cached(sourceId)?.revision ?: Long.MIN_VALUE) <= item.revision) {
            write(file(sourceId), json.encodeToString(CacheEntry(etag, raw)).toByteArray())
        }
        updateMeta { it.copy(applying = it.applying - sourceId, syncedAt = now) }
        return true
    }

    /** A tombstone: the item left the server. */
    fun remove(sourceId: String, now: Long) {
        if (!ID.matches(sourceId)) return
        AtomicFile(file(sourceId)).delete()
        updateMeta { it.copy(applying = it.applying - sourceId, syncedAt = now) }
    }

    /** The announced item is gone (404): stop showing it as applying and keep whatever copy there is. */
    fun settled(sourceId: String) {
        if (ID.matches(sourceId)) updateMeta { it.copy(applying = it.applying - sourceId) }
    }

    /**
     * A complete listing replaces the cache. [listed] are rows that decoded (id, body, etag); [unreadable] are ids the
     * server listed whose row this build could not read, so their last good copy stays. A listing is authoritative even
     * when it lowers a revision (a rebuilt server). Items in neither group are deleted only after every row was written,
     * so a failure part-way leaves a superset, never a hole. Returns how many items were removed.
     */
    fun replaceAll(listed: List<Triple<String, JsonObject, String?>>, unreadable: Set<String>, now: Long): Int {
        val live = mutableSetOf<String>()
        listed.forEach { (id, raw, etag) ->
            if (!ID.matches(id) || decode(id, raw) == null) return@forEach
            live += id
            // The row carries no ETag of its own: an unchanged body keeps the one it was fetched with (so the next
            // hint costs a 304, not a download), and an unchanged file is not rewritten.
            val current = entry(id)?.first
            val same = current != null && current.item == raw
            val kept = etag ?: if (same) current?.etag else null
            if (same && current?.etag == kept) return@forEach
            write(file(id), json.encodeToString(CacheEntry(kept, raw)).toByteArray())
        }
        live += unreadable
        val gone = ids().filter { it !in live }
        gone.forEach { AtomicFile(file(it)).delete() }
        updateMeta { meta -> CacheMeta(meta.applying.filter { it in live }.toSet(), now) }
        return gone.size
    }

    companion object {
        val ID = Regex("[A-Za-z0-9_-]{1,128}")

        /** The id of a row if it has a usable one (rows without one cannot be told apart and are skipped). */
        fun idOf(raw: JsonObject): String? = (raw["source_id"] as? JsonPrimitive)?.content?.takeIf { ID.matches(it) }
    }
}
