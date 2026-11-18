package io.github.junhcha.weaver.phone

import io.github.junhcha.weaver.transport.MobileApi
import java.util.UUID
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * History rows (태그정정, 거절, 실행, retractions, wiki field changes) are never edited; same rule as the web
 * editor, which the server enforces too. The sheet hides the editor for them so no request is even sent.
 */
fun PhoneThread.tagEditable(): Boolean = !isHistory()

data class TagEditResult(val changed: Int, val skipped: Int)

data class DictionaryEntry(val name: String, val kind: TagKind?, val aliases: List<String>, val usage: Int)

/** Tags matching [query] by name or alias, case-insensitive, never the ones already in [exclude]. */
fun suggestTags(query: String, tags: List<PhoneTag>, entries: List<DictionaryEntry>, exclude: Set<String>): List<PhoneTag> {
    val q = query.trim().lowercase()
    if (q.isEmpty()) return emptyList()
    val aliasHits = entries.filter { e -> e.aliases.any { it.lowercase().contains(q) } }.map { it.name }.toSet()
    return tags.filter { it.name !in exclude && (it.name.lowercase().contains(q) || it.name in aliasHits) }
        .sortedWith(compareByDescending<PhoneTag> { it.name.lowercase().startsWith(q) }.thenBy { it.name })
        .take(8)
}

/**
 * Tag writes against the paired server (web editor rules, ADR-040 contract). Every write is a POST because
 * `HttpURLConnection` cannot send PATCH and DELETE with a body is not reliable. Each call carries its own
 * `Idempotency-Key`; a retry of the *same* user action keeps it, so a lost response never edits twice.
 */
class TagEditor(private val api: MobileApi) {
    fun newKey(): String = UUID.randomUUID().toString()

    private fun names(key: String, values: Collection<String>) = JsonArray(values.map(::JsonPrimitive))

    private fun body(add: Collection<String>, remove: Collection<String>, kinds: Map<String, TagKind>, ids: List<String>? = null) =
        buildJsonObject {
            if (ids != null) put("thread_ids", JsonArray(ids.map(::JsonPrimitive)))
            put("add", names("add", add)); put("remove", names("remove", remove))
            put("kinds", JsonObject(kinds.mapValues { JsonPrimitive(it.value.wire) }))
        }

    fun editThread(threadId: String, add: Collection<String>, remove: Collection<String>,
                   kinds: Map<String, TagKind> = emptyMap(), key: String = newKey()): TagEditResult {
        api.write("POST", "/threads/$threadId/tags", body(add, remove, kinds), key)
        return TagEditResult(1, 0)
    }

    /** One request for every selected thread; the server skips rows that already have that state. */
    fun editBatch(threadIds: List<String>, add: Collection<String>, remove: Collection<String>,
                  kinds: Map<String, TagKind> = emptyMap(), key: String = newKey()): TagEditResult {
        val answer = api.write("POST", "/threads/tags/batch", body(add, remove, kinds, threadIds), key) as? JsonObject
        val changed = (answer?.get("changed") as? JsonPrimitive)?.content?.toIntOrNull() ?: threadIds.size
        val skipped = (answer?.get("skipped") as? JsonPrimitive)?.content?.toIntOrNull() ?: 0
        return TagEditResult(changed, skipped)
    }

    fun dictionary(): List<DictionaryEntry> = api.dictionary().mapNotNull { element ->
        val o = element as? JsonObject ?: return@mapNotNull null
        val name = (o["name"] as? JsonPrimitive)?.content ?: return@mapNotNull null
        DictionaryEntry(
            name, TagKind.fromWire((o["kind"] as? JsonPrimitive)?.takeIf { it.isString }?.content),
            (o["aliases"] as? JsonArray)?.mapNotNull { (it as? JsonPrimitive)?.content }.orEmpty(),
            (o["usage_count"] as? JsonPrimitive)?.content?.toIntOrNull() ?: 0,
        )
    }

    fun rename(entry: String, name: String, key: String = newKey()) {
        api.write("POST", "/dictionary/edit", buildJsonObject { put("entry", entry); put("name", name) }, key)
    }

    fun setKind(entry: String, kind: TagKind, key: String = newKey()) {
        api.write("POST", "/dictionary/edit", buildJsonObject { put("entry", entry); put("kind", kind.wire) }, key)
    }

    fun addAlias(entry: String, alias: String, key: String = newKey()) {
        api.write("POST", "/dictionary/aliases", buildJsonObject { put("entry", entry); put("alias", alias) }, key)
    }

    fun removeAlias(entry: String, alias: String, key: String = newKey()) {
        api.write("POST", "/dictionary/aliases/remove", buildJsonObject { put("entry", entry); put("alias", alias) }, key)
    }

    /** [kind] is required by the server when the two entries' kinds differ. */
    fun merge(entry: String, target: String, kind: TagKind? = null, key: String = newKey()) {
        api.write("POST", "/dictionary/merge", buildJsonObject {
            put("entry", entry); put("target", target); if (kind != null) put("kind", kind.wire)
        }, key)
    }
}
