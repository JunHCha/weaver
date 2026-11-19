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
 * Tag writes against the paired server (the edits endpoints, web editor rules). The idempotency key is the `client_id` inside the
 * body: a retry of the *same* user action keeps it, so a lost response never edits twice; the server answers a replay
 * with the first result. Rejections (history thread, new tag without a kind, merge needing a kind) come back as fixed
 * codes and are never retried.
 */
class TagEditor(private val api: MobileApi) {
    fun newKey(): String = UUID.randomUUID().toString()

    private fun strings(values: Collection<String>) = JsonArray(values.map(::JsonPrimitive))

    private fun tagBody(ids: List<String>, add: Collection<String>, remove: Collection<String>, kinds: Map<String, TagKind>, key: String) =
        buildJsonObject {
            put("client_id", key)
            put("thread_ids", strings(ids))
            if (add.isNotEmpty()) put("add", strings(add))
            if (remove.isNotEmpty()) put("remove", strings(remove))
            if (kinds.isNotEmpty()) put("kinds", JsonObject(kinds.mapValues { JsonPrimitive(it.value.wire) }))
        }

    private fun result(operation: JsonObject): JsonObject? = operation["result"] as? JsonObject

    /** One request for one or many threads; the server skips rows that already have that state. */
    fun editTags(threadIds: List<String>, add: Collection<String>, remove: Collection<String>,
                 kinds: Map<String, TagKind> = emptyMap(), key: String = newKey()): TagEditResult {
        val result = result(api.edit("/tags", tagBody(threadIds, add, remove, kinds, key)))
        val changed = (result?.get("changed") as? JsonPrimitive)?.content?.toIntOrNull() ?: threadIds.size
        val skipped = (result?.get("skipped") as? JsonPrimitive)?.content?.toIntOrNull() ?: 0
        return TagEditResult(changed, skipped)
    }

    fun editThread(threadId: String, add: Collection<String>, remove: Collection<String>,
                   kinds: Map<String, TagKind> = emptyMap(), key: String = newKey()) = editTags(listOf(threadId), add, remove, kinds, key)

    fun editBatch(threadIds: List<String>, add: Collection<String>, remove: Collection<String>,
                  kinds: Map<String, TagKind> = emptyMap(), key: String = newKey()) = editTags(threadIds, add, remove, kinds, key)

    fun dictionary(): List<DictionaryEntry> = api.dictionary().mapNotNull { element ->
        val o = element as? JsonObject ?: return@mapNotNull null
        val name = (o["name"] as? JsonPrimitive)?.content ?: return@mapNotNull null
        DictionaryEntry(
            name, TagKind.fromWire((o["kind"] as? JsonPrimitive)?.takeIf { it.isString }?.content),
            (o["aliases"] as? JsonArray)?.mapNotNull { (it as? JsonPrimitive)?.content }.orEmpty(),
            (o["usage_count"] as? JsonPrimitive)?.content?.toIntOrNull() ?: 0,
        )
    }

    private fun dictionary(path: String, key: String, vararg fields: Pair<String, String?>) {
        api.edit("/dictionary/$path", buildJsonObject {
            put("client_id", key)
            fields.forEach { (name, value) -> if (value != null) put(name, value) }
        })
    }

    fun rename(entry: String, name: String, key: String = newKey()) = dictionary("rename", key, "entry" to entry, "name" to name)
    fun setKind(entry: String, kind: TagKind, key: String = newKey()) = dictionary("kind", key, "entry" to entry, "kind" to kind.wire)
    fun addAlias(entry: String, alias: String, key: String = newKey()) = dictionary("alias-add", key, "entry" to entry, "alias" to alias)
    fun removeAlias(entry: String, alias: String, key: String = newKey()) = dictionary("alias-remove", key, "entry" to entry, "alias" to alias)

    /** [kind] is required by the server when the two entries' kinds differ (422 `kind_choice_required`). */
    fun merge(entry: String, target: String, kind: TagKind? = null, key: String = newKey()) =
        dictionary("merge", key, "entry" to entry, "target" to target, "kind" to kind?.wire)
}
