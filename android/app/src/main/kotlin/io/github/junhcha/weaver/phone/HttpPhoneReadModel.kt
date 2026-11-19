package io.github.junhcha.weaver.phone

import io.github.junhcha.weaver.transport.ApiException
import io.github.junhcha.weaver.transport.InvalidResponseException
import io.github.junhcha.weaver.transport.MobileApi
import io.github.junhcha.weaver.transport.PinMismatchException
import io.github.junhcha.weaver.transport.UnreachableException
import java.io.File
import java.io.FileOutputStream
import java.time.Instant
import java.time.LocalDate
import java.util.UUID
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put

/**
 * The real read model: Fabric threads, tags and Wiki pages of the paired server (fabric/threads, fabric/tags
 * and wiki/pages under `/mobile/v1`, ADR-040 contract). The last complete copy is cached on disk, so the screens still work when the
 * Mac is off or the VPN is down; [offlineSince] then holds when that copy was fetched. A failed or partial fetch
 * never replaces the cache and never reads as "everything was deleted".
 */
class HttpPhoneReadModel(
    private val api: () -> MobileApi?,
    private val cache: File,
    private val now: () -> Instant = Instant::now,
    /** Reports stable fetch/parse codes and skipped row counts; a clean snapshot reports null. */
    private val onProblem: (String?) -> Unit = {},
) : PhoneReadModel {
    @Volatile override var offlineSince: Instant? = null
        private set

    override suspend fun snapshot(): PhoneSnapshot = withContext(Dispatchers.IO) {
        var failure: String? = null
        try {
            val client = api()
            if (client != null) {
                val fresh = fetch(client)
                val snapshot = counted(fresh)
                offlineSince = null
                runCatching { write(fresh) }
                return@withContext snapshot
            }
        } catch (error: CancellationException) { throw error
        } catch (error: Exception) {
            failure = problemCode(error)
            onProblem(failure)
        }
        val cached = runCatching { json.parseToJsonElement(cache.readText()).jsonObject }.getOrNull()
        if (cached == null) {
            offlineSince = null
            if (failure == null && cache.exists()) onProblem("invalid_cache")
            return@withContext PhoneSnapshot(emptyList(), emptyList(), emptyList())
        }
        offlineSince = Instant.ofEpochMilli(cache.lastModified())
        try { counted(cached, report = failure == null) }
        catch (error: CancellationException) { throw error }
        catch (_: Exception) {
            if (failure == null) onProblem("invalid_cache")
            PhoneSnapshot(emptyList(), emptyList(), emptyList())
        }
    }

    private fun problemCode(error: Exception): String = when (error) {
        is ApiException -> error.code
        is PinMismatchException -> "pin_mismatch"
        is InvalidResponseException -> "invalid_read_response"
        is UnreachableException -> "unreachable"
        is java.io.IOException -> "unreachable"
        else -> "invalid_read_response"
    }

    /**
     * Opens a page: its body is fetched with `If-None-Match` and cached next to the snapshot, so a page read once also opens
     * offline. A failed fetch falls back to the cached body; none cached means null (the viewer says so).
     */
    override suspend fun wikiBody(page: WikiPage): String? = withContext(Dispatchers.IO) {
        val file = File(cache.parentFile, "${cache.nameWithoutExtension}-wiki/${page.id}.json")
        val cached = runCatching { json.parseToJsonElement(file.readText()).jsonObject }.getOrNull()
        try {
            val client = api() ?: return@withContext cached?.str("body")
            val etag = cached?.str("etag")?.takeIf { it.isNotEmpty() }
            val fresh = client.wikiPage(page.id, etag)
                ?: return@withContext cached?.str("body") // 304: the cached copy is current
            val body = json.parseToJsonElement(fresh.body.decodeToString()).jsonObject.str("body")
            if (body == null) {
                onProblem("invalid_wiki_body")
            } else {
                runCatching {
                    file.parentFile?.mkdirs()
                    file.writeText(buildJsonObject { put("etag", fresh.etag ?: ""); put("body", body) }.toString())
                }
                return@withContext body
            }
        } catch (error: CancellationException) { throw error
        } catch (error: Exception) { onProblem(problemCode(error)) }
        return@withContext cached?.str("body")
    }

    private fun counted(root: JsonObject, report: Boolean = true): PhoneSnapshot {
        val unknown = java.util.concurrent.atomic.AtomicInteger()
        val invalid = java.util.concurrent.atomic.AtomicInteger()
        val invalidTags = java.util.concurrent.atomic.AtomicInteger()
        val invalidPages = java.util.concurrent.atomic.AtomicInteger()
        val snapshot = parse(root, unknown, invalid, invalidTags, invalidPages)
        val codes = listOf("unknown_thread_kind" to unknown, "invalid_thread" to invalid,
            "invalid_tag" to invalidTags, "invalid_wiki_page" to invalidPages)
            .filter { it.second.get() > 0 }.joinToString(",") { "${it.first}:${it.second.get()}" }
        if (report) onProblem(codes.takeIf { it.isNotEmpty() })
        return snapshot
    }

    private fun fetch(client: MobileApi): JsonObject {
        fun all(next: (String?, Int) -> io.github.junhcha.weaver.transport.Page) =
            JsonArray(io.github.junhcha.weaver.transport.Paging.all(io.github.junhcha.weaver.transport.HttpsMobileApi.PAGE, next))
        return buildJsonObject {
            put("threads", all(client::threads))
            put("tags", JsonArray(client.tags()))
            put("wiki", all(client::wikiPages))
            put("fetched_at", now().toString())
        }
    }

    private fun write(snapshot: JsonObject) {
        cache.parentFile?.mkdirs()
        val temp = File(cache.parentFile, ".${UUID.randomUUID()}.tmp")
        try {
            FileOutputStream(temp).use { it.write(snapshot.toString().toByteArray()); it.fd.sync() }
            check(temp.renameTo(cache)) { "cache_write_failed" }
        } finally { temp.delete() }
    }

    companion object {
        private val json = Json { ignoreUnknownKeys = true }

        private class UnknownKind : IllegalArgumentException("unknown_kind")

        fun parse(
            root: JsonObject, unknown: java.util.concurrent.atomic.AtomicInteger = java.util.concurrent.atomic.AtomicInteger(),
            invalid: java.util.concurrent.atomic.AtomicInteger = java.util.concurrent.atomic.AtomicInteger(),
            invalidTags: java.util.concurrent.atomic.AtomicInteger = java.util.concurrent.atomic.AtomicInteger(),
            invalidPages: java.util.concurrent.atomic.AtomicInteger = java.util.concurrent.atomic.AtomicInteger(),
        ): PhoneSnapshot = PhoneSnapshot(
            // A row the phone cannot understand is skipped but counted, so the connection card can say so.
            threads = root["threads"]!!.jsonArray.mapNotNull {
                try { thread(it.jsonObject) } catch (_: UnknownKind) { unknown.incrementAndGet(); null }
                catch (_: Exception) { invalid.incrementAndGet(); null }
            },
            tags = root["tags"]!!.jsonArray.mapNotNull {
                try { tag(it.jsonObject).also { tag -> require(tag.name.isNotBlank()) } }
                catch (_: Exception) { invalidTags.incrementAndGet(); null }
            },
            wikiPages = root["wiki"]!!.jsonArray.mapNotNull {
                try { page(it.jsonObject) } catch (_: Exception) { invalidPages.incrementAndGet(); null }
            },
        )

        private fun JsonObject.str(key: String): String? = (this[key] as? JsonPrimitive)?.takeIf { it !is JsonNull }?.contentOrNull
        private fun JsonObject.strings(key: String): List<String> =
            (this[key] as? JsonArray)?.mapNotNull { (it as? JsonPrimitive)?.contentOrNull }.orEmpty()

        private fun kind(korean: String) = when (korean) {
            "요청" -> ThreadKind.REQUEST; "결정" -> ThreadKind.DECISION; "일정" -> ThreadKind.SCHEDULE
            "기록" -> ThreadKind.RECORD; "태그정정" -> ThreadKind.TAG_CORRECTION; "거절" -> ThreadKind.REJECTION
            "실행" -> ThreadKind.EXECUTION
            else -> throw UnknownKind()
        }

        private fun tagKind(wire: String?) = when (wire) {
            "project" -> TagKind.PROJECT; "area" -> TagKind.AREA; "person" -> TagKind.PERSON
            "organization" -> TagKind.ORGANIZATION; "topic" -> TagKind.TOPIC
            else -> null
        }

        /**
         * `/read/threads` item: the web Thread plus the server's `presentation`, `open`, `retracted`, `effective_deadline`,
         * `effective_schedule` (an object), `mentions` and the display `source`. Taken as given, never recomputed.
         */
        private fun thread(o: JsonObject): PhoneThread {
            val field = o.str("field")
            val correction = (o["correction"] as? JsonObject)?.let {
                TagCorrection(it.str("target")!!, it.strings("add"), it.strings("remove"))
            }
            val schedule = (o["effective_schedule"] as? JsonObject) ?: (o["value"] as? JsonObject)?.takeIf { field == "schedule" }
            val deadline = o.str("effective_deadline") ?: o.str("value")?.takeIf { field == "deadline" }
            return PhoneThread(
                id = o.str("thread_id")!!,
                seq = o.str("seq")!!.toLong(),
                kind = kind(o.str("kind")!!),
                content = o.str("content").orEmpty(),
                occurredAt = Instant.parse(o.str("occurred_at")!!),
                recordedAt = Instant.parse(o.str("recorded_at")!!),
                source = o.str("source") ?: "기록",
                counterpart = o.str("counterpart"),
                tags = o.strings("tags"),
                direction = when (o.str("direction")) { "받음" -> Direction.RECEIVED; "보냄" -> Direction.SENT; else -> null },
                deadline = deadline?.let { runCatching { LocalDate.parse(it) }.getOrNull() },
                scheduleAt = schedule?.str("start")?.let { runCatching { Instant.parse(it) }.getOrNull() },
                field = field,
                presentation = when (o.str("presentation")) { "fact" -> Presentation.FACT; "history" -> Presentation.HISTORY; else -> null },
                resolves = o.str("resolves"), retracts = o.str("retracts"), updates = o.str("updates"),
                executes = o.str("executes"), correction = correction,
                open = (o["open"] as? JsonPrimitive)?.contentOrNull?.toBooleanStrictOrNull(),
                retracted = (o["retracted"] as? JsonPrimitive)?.contentOrNull?.toBooleanStrictOrNull(),
                mentions = if (o["mentions"] is JsonArray) o.strings("mentions") else null,
            )
        }

        private fun tag(o: JsonObject) = PhoneTag(o.str("name")!!, tagKind(o.str("kind")))

        /** List entry without a body (`/read/wiki/pages`); [updated] is the Seoul calendar day of `updated_at`. */
        private fun page(o: JsonObject): WikiPage {
            val front = o["frontmatter"] as? JsonObject
            return WikiPage(
                id = o.str("id")!!,
                title = o.str("title")!!,
                kind = tagKind(o.str("kind")) ?: TagKind.TOPIC,
                area = o.str("area"),
                updated = Instant.parse(o.str("updated_at")!!).atZone(SeoulZone).toLocalDate(),
                frontmatter = WikiFrontmatter(
                    status = front?.str("status"),
                    deadline = front?.str("deadline")?.let { runCatching { LocalDate.parse(it) }.getOrNull() },
                    assignees = front?.strings("assignees").orEmpty(),
                    next = front?.str("next"),
                ),
                body = o.str("body").orEmpty(),
                sha256 = o.str("sha256"),
            )
        }
    }
}
