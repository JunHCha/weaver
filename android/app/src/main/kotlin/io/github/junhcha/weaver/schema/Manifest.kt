package io.github.junhcha.weaver.schema

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Capture channel shared by Manifest and Proposal.source (schema/ $defs Channel). */
@Serializable
enum class Channel {
    @SerialName("call") CALL,
    @SerialName("voice_memo") VOICE_MEMO,
    @SerialName("kakao") KAKAO,
    @SerialName("share_text") SHARE_TEXT,
}

/** Payload kind: `fleece` is raw audio, `text` is shared text. */
@Serializable
enum class CaptureKind {
    @SerialName("fleece") FLEECE,
    @SerialName("text") TEXT,
}

/** Shepherd → outbox/<capture_id>.json. Mirrors schema/Manifest.schema.json. */
@Serializable
data class Manifest(
    @SerialName("schema_version") val schemaVersion: Int,
    @SerialName("capture_id") val captureId: String,
    val kind: CaptureKind,
    val channel: Channel,
    val payload: String,
    val sha256: String,
    val size: Long,
    @SerialName("captured_at") val capturedAt: String,
    @SerialName("occurred_at") val occurredAt: String?,
    val hints: Hints,
) {
    init {
        Rules.schemaVersion(schemaVersion)
        Rules.prefixedId("cap", captureId, "capture_id")
        Rules.payloadRef(payload, "payload")
        Rules.digest(sha256, "sha256")
        Rules.offset(size, "size")
        Rules.timestamp(capturedAt, "captured_at")
        occurredAt?.let { Rules.timestamp(it, "occurred_at") }
    }

    @Serializable
    data class Hints(val counterpart: String?) {
        init {
            counterpart?.let { Rules.nonEmpty(it, "hints.counterpart") }
        }
    }
}
