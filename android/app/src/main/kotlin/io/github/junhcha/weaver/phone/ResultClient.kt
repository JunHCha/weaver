package io.github.junhcha.weaver.phone

import io.github.junhcha.weaver.schema.DocumentSummary
import io.github.junhcha.weaver.schema.DocumentView
import io.github.junhcha.weaver.schema.Evidence
import io.github.junhcha.weaver.schema.FeedbackChip
import io.github.junhcha.weaver.schema.FeedbackItem
import io.github.junhcha.weaver.schema.label
import io.github.junhcha.weaver.schema.RefLinkView
import io.github.junhcha.weaver.schema.ResultCard
import io.github.junhcha.weaver.schema.SourceLogEntry
import io.github.junhcha.weaver.schema.TaskItem
import io.github.junhcha.weaver.schema.TreeNodeView
import io.github.junhcha.weaver.transport.ApiException
import io.github.junhcha.weaver.transport.InvalidResponseException
import io.github.junhcha.weaver.transport.MobileApi
import io.github.junhcha.weaver.transport.PinMismatchException
import io.github.junhcha.weaver.transport.UnreachableException
import java.io.File
import java.security.MessageDigest
import java.time.Instant
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.KSerializer
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put

/**
 * A value read from the server or, when it could not be read, from the last copy; [offlineSince] is when that copy was
 * fetched. [unavailable] means the read failed and there is no copy at all: the empty [value] is not "nothing there".
 */
data class Cached<T>(val value: T, val offlineSince: Instant?, val unavailable: Boolean = false)

/** How sending or undoing feedback ended. [Rejected] carries the server's stable code; [Unreachable] may be sent again with the same key. */
sealed interface FeedbackOutcome {
    data class Done(val item: FeedbackItem) : FeedbackOutcome
    data class Rejected(val code: String) : FeedbackOutcome
    data object Unreachable : FeedbackOutcome
}

/** One page of `GET /read/sources`. */
data class SourcePage(val items: List<SourceLogEntry>, val nextCursor: String?)

/**
 * Reads of the results API (tree, documents, evidence, cards, tasks, sources; server feature `results`). The last good copy
 * of each list and document lives under [cacheDir], so the 영역 tab still opens offline. A failed fetch never replaces a copy
 * and never reads as "everything was deleted". Evidence is never cached: it carries whole source texts.
 */
class ResultClient(
    private val api: () -> MobileApi?,
    private val cacheDir: File,
    private val now: () -> Instant = Instant::now,
    /** Reports a stable code for a failed read (server error code, `unreachable`, `invalid_read_response`); a good read reports null. */
    private val onProblem: (String?) -> Unit = {},
) {
    private val json = Json { ignoreUnknownKeys = true }

    /** True when the paired server offers `features.results`; the last answer is remembered for offline starts. */
    suspend fun enabled(): Boolean = feature("results", "results-enabled")

    /** True when the paired server offers `features.feedback` (document feedback); remembered like [enabled]. */
    suspend fun feedbackEnabled(): Boolean = feature("feedback", "feedback-enabled")

    private suspend fun feature(name: String, remembered: String): Boolean = withContext(Dispatchers.IO) {
        val flag = File(cacheDir, remembered)
        try {
            val client = api() ?: return@withContext flag.readOrNull() == "true"
            val on = ((client.capabilities()["features"] as? JsonObject)?.get(name) as? JsonPrimitive)?.contentOrNull == "true"
            write(flag, on.toString())
            on
        } catch (error: CancellationException) { throw error
        } catch (_: Exception) { flag.readOrNull() == "true" }
    }

    suspend fun tree(): Cached<List<TreeNodeView>> =
        list(File(cacheDir, "tree.json"), TreeNodeView.serializer()) { it.tree() }

    /** Notes related to a tree node ([path] is the node's path). */
    suspend fun references(path: String): Cached<List<RefLinkView>> =
        list(File(cacheDir, "references/${digest(path)}.json"), RefLinkView.serializer()) { it.references(path) }

    /** Document list (id, kind, key, title) used to resolve wiki links to documents. */
    suspend fun documents(): Cached<List<DocumentSummary>> =
        list(File(cacheDir, "documents.json"), DocumentSummary.serializer()) { it.documents() }

    /** Cards of [window] around [at] (an instant; the server's now when null). The saved copy is per window, whatever [at] was. */
    suspend fun cards(window: String = "today", at: String? = null): Cached<List<ResultCard>> {
        val stamp = mutableMapOf<String, JsonElement>()
        // The copy is only good for the day (in the server's zone) it was cut for: another day's cards must not pass as today's.
        val sameDay = { saved: JsonObject ->
            runCatching {
                val zone = java.time.ZoneId.of((saved["timezone"] as JsonPrimitive).content)
                val day = (at?.let(Instant::parse) ?: now()).atZone(zone).toLocalDate().toString()
                day == (saved["anchor"] as JsonPrimitive).content
            }.getOrDefault(false)
        }
        return list(File(cacheDir, "cards-$window.json"), ResultCard.serializer(), stamp, sameDay) {
            val body = it.cards(window, at)
            stamp["anchor"] = body["today"] as? JsonPrimitive ?: throw IllegalStateException("today")
            stamp["timezone"] = body["timezone"] as? JsonPrimitive ?: throw IllegalStateException("timezone")
            (body["items"] as? JsonArray)?.toList() ?: throw IllegalStateException("items")
        }
    }

    suspend fun tasks(): Cached<List<TaskItem>> =
        list(File(cacheDir, "tasks.json"), TaskItem.serializer()) {
            (it.tasks()["items"] as? JsonArray)?.toList() ?: throw IllegalStateException("items")
        }

    /** One page of the source log; null when the server cannot be reached or answers badly (nothing is cached). */
    suspend fun sources(cursor: String?, limit: Int = 50, filters: Map<String, String> = emptyMap()): SourcePage? =
        withContext(Dispatchers.IO) {
            try {
                val page = (api() ?: return@withContext null).sources(cursor, limit, filters)
                SourcePage(page.items.map { json.decodeFromJsonElement(SourceLogEntry.serializer(), it) }, page.nextCursor)
                    .also { onProblem(null) }
            } catch (error: CancellationException) { throw error
            } catch (error: Exception) { report(error); null }
        }

    /**
     * The compiled document, fetched with `If-None-Match` and cached with its ETag. A 304 gives the cached copy as current;
     * an unreachable server gives it with [Cached.offlineSince]; none cached means null. A 404 drops the copy.
     */
    suspend fun document(id: String): Cached<DocumentView>? = withContext(Dispatchers.IO) {
        if (!id.matches(ID)) return@withContext null
        val file = File(cacheDir, "documents/$id.json")
        val cached = file.readOrNull()?.let { runCatching { json.parseToJsonElement(it).jsonObject }.getOrNull() }
        val copy = cached?.let { c ->
            runCatching {
                Copy(json.decodeFromJsonElement(DocumentView.serializer(), c["view"]!!),
                    (c["etag"] as? JsonPrimitive)?.contentOrNull, Instant.parse((c["at"] as JsonPrimitive).content))
            }.getOrNull()
        }
        try {
            val client = api() ?: return@withContext copy?.let { Cached(it.view, it.at) }
            val fresh = client.document(id, copy?.etag?.takeIf { it.isNotEmpty() })
            if (fresh == null) { onProblem(null); return@withContext copy?.let { Cached(it.view, null) } }
            val view = json.parseToJsonElement(fresh.body.decodeToString())
            val decoded = json.decodeFromJsonElement(DocumentView.serializer(), view)
            runCatching {
                write(file, buildJsonObject { put("etag", fresh.etag ?: ""); put("at", now().toString()); put("view", view) }.toString())
            }
            onProblem(null)
            Cached(decoded, null)
        } catch (error: CancellationException) { throw error
        } catch (error: ApiException) {
            report(error)
            if (error.status == 404) { file.delete(); null }
            else copy?.let { Cached(it.view, it.at) }
        } catch (error: Exception) {
            report(error)
            copy?.let { Cached(it.view, it.at) }
        }
    }

    /** The sentence's Threads and whole source texts; null when it cannot be read right now (never cached). */
    suspend fun evidence(id: String, bulletId: String): Evidence? = withContext(Dispatchers.IO) {
        try {
            val client = api() ?: return@withContext null
            json.decodeFromString(Evidence.serializer(), client.evidence(id, bulletId).decodeToString()).also { onProblem(null) }
        } catch (error: CancellationException) { throw error
        } catch (error: Exception) { report(error); null }
    }

    /** The feedback sent on [documentId], oldest first as the server lists it; null when it cannot be read right now (never cached). */
    suspend fun feedback(documentId: String): List<FeedbackItem>? = withContext(Dispatchers.IO) {
        try {
            val client = api() ?: return@withContext null
            val items = client.feedback(documentId)["items"] as? JsonArray ?: throw InvalidResponseException()
            json.decodeFromJsonElement(ListSerializer(FeedbackItem.serializer()), items).also { onProblem(null) }
        } catch (error: CancellationException) { throw error
        } catch (error: Exception) { report(error); null }
    }

    /**
     * Sends a note through the journaled edit path. [clientId] is the idempotency key of this input: a retry of the same
     * input must pass the same one, so a lost response never records the note twice. The note never completes a task.
     */
    suspend fun sendFeedback(documentId: String, bulletId: String?, text: String, chips: List<FeedbackChip>, clientId: String): FeedbackOutcome =
        edit { it.postFeedback(buildJsonObject {
            put("client_id", clientId)
            put("document_id", documentId)
            if (bulletId != null) put("bullet_id", bulletId)
            put("text", text)
            put("chips", JsonArray(chips.map { JsonPrimitive(it.label) }))
        }) }

    /** Undoes an applied or pending note; [clientId] keeps a retry idempotent. */
    suspend fun undoFeedback(feedbackId: String, clientId: String): FeedbackOutcome =
        edit { it.undoFeedback(buildJsonObject { put("client_id", clientId); put("feedback_id", feedbackId) }) }

    private suspend fun edit(call: (MobileApi) -> JsonObject): FeedbackOutcome = withContext(Dispatchers.IO) {
        try {
            val client = api() ?: return@withContext FeedbackOutcome.Unreachable
            val operation = call(client)
            val result = operation["result"] as? JsonObject
            if ((operation["state"] as? JsonPrimitive)?.contentOrNull != "applied" || result == null) throw InvalidResponseException()
            FeedbackOutcome.Done(json.decodeFromJsonElement(FeedbackItem.serializer(), result)).also { onProblem(null) }
        } catch (error: CancellationException) { throw error
        } catch (error: ApiException) { report(error); if (error.retryable) FeedbackOutcome.Unreachable else FeedbackOutcome.Rejected(error.code)
        } catch (error: Exception) { report(error); FeedbackOutcome.Unreachable }
    }

    /** [stamp] is filled by [fetch] and saved beside the items; a saved copy is used only when [usable] accepts it. */
    private suspend fun <T> list(
        file: File, item: KSerializer<T>, stamp: Map<String, JsonElement> = emptyMap(), usable: (JsonObject) -> Boolean = { true },
        fetch: (MobileApi) -> List<JsonElement>,
    ): Cached<List<T>> =
        withContext(Dispatchers.IO) {
            val saved = file.readOrNull()?.let { runCatching { json.parseToJsonElement(it).jsonObject }.getOrNull() }?.takeIf(usable)
            val copy = saved?.let { s ->
                runCatching {
                    json.decodeFromJsonElement(ListSerializer(item), s["items"]!!) to Instant.parse((s["at"] as JsonPrimitive).content)
                }.getOrNull()
            }
            try {
                val raw = fetch(api() ?: throw NotPaired())
                val decoded = json.decodeFromJsonElement(ListSerializer(item), JsonArray(raw))
                runCatching { write(file, buildJsonObject { put("at", now().toString()); put("items", JsonArray(raw)); stamp.forEach { (k, v) -> put(k, v) } }.toString()) }
                onProblem(null)
                Cached(decoded, null)
            } catch (error: CancellationException) { throw error
            } catch (error: Exception) {
                report(error)
                if (copy != null) Cached(copy.first, copy.second) else Cached(emptyList(), now(), unavailable = true)
            }
        }

    private class NotPaired : RuntimeException("not_paired")

    /** Nothing paired is not a problem to report; every other failure gets one stable code. */
    private fun report(error: Exception) {
        if (error is NotPaired) return
        onProblem(when (error) {
            is ApiException -> error.code
            is PinMismatchException -> "pin_mismatch"
            is InvalidResponseException -> "invalid_read_response"
            is UnreachableException -> "unreachable"
            is java.io.IOException -> "unreachable"
            else -> "invalid_read_response"
        })
    }

    private fun write(file: File, text: String) {
        file.parentFile?.mkdirs()
        val temp = File(file.parentFile, file.name + ".tmp")
        temp.writeText(text)
        java.nio.file.Files.move(temp.toPath(), file.toPath(), java.nio.file.StandardCopyOption.REPLACE_EXISTING)
    }

    private fun File.readOrNull(): String? = runCatching { readText() }.getOrNull()

    private fun digest(text: String) =
        MessageDigest.getInstance("SHA-256").digest(text.toByteArray()).take(12).joinToString("") { "%02x".format(it) }

    private class Copy(val view: DocumentView, val etag: String?, val at: Instant)

    private companion object {
        val ID = Regex("[A-Za-z0-9_-]+")
    }
}
