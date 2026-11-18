package io.github.junhcha.weaver.phone

import io.github.junhcha.weaver.transport.MobileApi
import java.io.File
import java.io.FileOutputStream
import java.time.Instant
import java.time.LocalDate
import java.util.UUID
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
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
    /** Called after every read with a stable code when rows could not be understood (e.g. `unknown_thread_kind:2`), else null. */
    private val onProblem: (String?) -> Unit = {},
) : PhoneReadModel {
    @Volatile override var offlineSince: Instant? = null
        private set

    override suspend fun snapshot(): PhoneSnapshot {
        val client = api()
        val fresh = client?.let { runCatching { fetch(it) }.getOrNull() }
        if (fresh != null) {
            offlineSince = null
            runCatching { write(fresh) }
            return counted(fresh)
        }
        val cached = runCatching { json.parseToJsonElement(cache.readText()).jsonObject }.getOrNull()
        if (cached == null) {
            offlineSince = null
            return PhoneSnapshot(emptyList(), emptyList(), emptyList())
        }
        offlineSince = Instant.ofEpochMilli(cache.lastModified())
        return runCatching { counted(cached) }.getOrDefault(PhoneSnapshot(emptyList(), emptyList(), emptyList()))
    }

    private fun counted(root: JsonObject): PhoneSnapshot {
        val unknown = java.util.concurrent.atomic.AtomicInteger()
        val invalid = java.util.concurrent.atomic.AtomicInteger()
        val snapshot = parse(root, unknown, invalid)
        onProblem(when {
            unknown.get() > 0 -> "unknown_thread_kind:${unknown.get()}"
            invalid.get() > 0 -> "invalid_thread:${invalid.get()}"
            else -> null
        })
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
        private const val MAX_PAGES = 500
        private val json = Json { ignoreUnknownKeys = true }

        private class UnknownKind : IllegalArgumentException("unknown_kind")

        fun parse(
            root: JsonObject, unknown: java.util.concurrent.atomic.AtomicInteger = java.util.concurrent.atomic.AtomicInteger(),
            invalid: java.util.concurrent.atomic.AtomicInteger = java.util.concurrent.atomic.AtomicInteger(),
        ): PhoneSnapshot = PhoneSnapshot(
            // A row the phone cannot understand is skipped but counted, so the connection card can say so.
            threads = root["threads"]!!.jsonArray.mapNotNull {
                try { thread(it.jsonObject) } catch (_: UnknownKind) { unknown.incrementAndGet(); null }
                catch (_: Exception) { invalid.incrementAndGet(); null }
            },
            tags = root["tags"]!!.jsonArray.mapNotNull { runCatching { tag(it.jsonObject) }.getOrNull() }
                .filter { it.name.isNotBlank() },
            wikiPages = root["wiki"]!!.jsonArray.mapNotNull { runCatching { page(it.jsonObject) }.getOrNull() },
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

        /** Web `Thread` fields plus the display `source` label; `field`/`value` carry deadline and schedule. */
        private fun thread(o: JsonObject): PhoneThread {
            val field = o.str("field")
            val value = o.str("value")
            val correction = (o["correction"] as? JsonObject)?.let {
                TagCorrection(it.str("target")!!, it.strings("add"), it.strings("remove"))
            }
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
                deadline = if (field == "deadline" && value != null) runCatching { LocalDate.parse(value) }.getOrNull() else null,
                scheduleAt = if (field == "schedule" && value != null) runCatching { Instant.parse(value) }.getOrNull() else null,
                field = field,
                presentation = when (o.str("presentation")) { "fact" -> Presentation.FACT; "history" -> Presentation.HISTORY; else -> null },
                resolves = o.str("resolves"), retracts = o.str("retracts"), updates = o.str("updates"),
                executes = o.str("executes"), correction = correction,
            )
        }

        private fun tag(o: JsonObject) = PhoneTag(o.str("name")!!, tagKind(o.str("kind")))

        private fun page(o: JsonObject): WikiPage {
            val front = o["frontmatter"] as? JsonObject
            return WikiPage(
                id = o.str("page_id")!!,
                title = o.str("title")!!,
                kind = tagKind(o.str("kind")) ?: TagKind.TOPIC,
                area = o.str("area"),
                updated = LocalDate.parse(o.str("updated")!!),
                frontmatter = WikiFrontmatter(
                    status = front?.str("status"),
                    deadline = front?.str("deadline")?.let { runCatching { LocalDate.parse(it) }.getOrNull() },
                    assignees = front?.strings("assignees").orEmpty(),
                    next = front?.str("next"),
                ),
                body = o.str("body").orEmpty(),
            )
        }
    }
}
