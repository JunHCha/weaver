package io.github.junhcha.weaver.ui

import androidx.compose.runtime.saveable.Saver
import io.github.junhcha.weaver.schema.Verdict
import io.github.junhcha.weaver.schema.WeaverCodec
import kotlinx.serialization.Serializable

/** Saved instance state only: no durable draft database or new exchange files. */
@Serializable
internal data class ReviewInput(
    val path: String = "/summary",
    val value: String = "",
    val edits: List<Verdict.Edit> = emptyList(),
    val instruction: String = "",
    val answers: Map<String, String> = emptyMap(),
    val reason: Verdict.RejectReason = Verdict.RejectReason.OTHER,
    val confirmations: Set<String> = emptySet(),
) {
    companion object {
        val saver = Saver<ReviewInput, String>(
            save = { WeaverCodec.json.encodeToString(it) },
            restore = { WeaverCodec.json.decodeFromString<ReviewInput>(it) },
        )
    }
}
