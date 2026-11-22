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
