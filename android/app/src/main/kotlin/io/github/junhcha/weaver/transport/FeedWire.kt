package io.github.junhcha.weaver.transport

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

/**
 * `GET /changes` envelope (hand-written; contract tests read `schema/mobile/fixtures/changes*.json`).
 * The feed only hints which resource changed: Alert bodies come from `/alerts`, maps from `/read/pipeline`,
 * Wiki pages from `/read/wiki`. The client feed loop is not part of this model.
 */
@Serializable
data class FeedEnvelope(
    @SerialName("feed_epoch") val feedEpoch: String,
    val items: List<FeedEntry>,
    @SerialName("next_cursor") val nextCursor: String,
    @SerialName("has_more") val hasMore: Boolean,
)

@Serializable
data class FeedEntry(
    val seq: Long,
    val kind: String,
    val id: String,
    val revision: Long? = null,
    val op: String,
    val body: JsonElement? = null,
) {
    /** Only a tombstone means the resource was removed; a kind that leaves the feed never does. */
    val removed: Boolean get() = op == "tombstone"
}

/** Kinds every phone build understands. A phone asks for newer kinds with `?kinds=`; without it the server sends only these. */
val LEGACY_FEED_KINDS: Set<String> = setOf("review", "action", "fabric")

/** Tolerates additive fields in the feed envelope (the strict hand-written models are checked against fixtures in tests). */
internal val feedJson = kotlinx.serialization.json.Json { ignoreUnknownKeys = true }

/** Kinds this build can apply. A kind outside this set stops the checkpoint at its entry. */
val SUPPORTED_FEED_KINDS: Set<String> = setOf("review", "action", "fabric", "alert", "pipeline", "wiki")

/** `<epoch>.<seq>.<pin>`: opaque to everything except the one place that must stop short of an entry. */
object FeedCursor {
    private val shape = Regex("^(ep_[0-9a-f]{32})\\.(\\d+)(?:\\.([0-9a-f]{8}))?$")

    fun epoch(cursor: String): String? = shape.matchEntire(cursor)?.groupValues?.get(1)

    /** The same cursor moved back to [seq] (inclusive: entries up to it were applied), or null if the cursor has another shape. */
    fun upTo(cursor: String, seq: Long): String? {
        val match = shape.matchEntire(cursor) ?: return null
        val pin = match.groupValues[3]
        return match.groupValues[1] + "." + seq + (if (pin.isEmpty()) "" else ".$pin")
    }
}
