package io.github.junhcha.weaver.schema

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** PC → receipts/<capture_id>.json. Mirrors schema/Receipt.schema.json. */
@Serializable
data class Receipt(
    @SerialName("schema_version") val schemaVersion: Int,
    @SerialName("capture_id") val captureId: String,
    @SerialName("source_id") val sourceId: String,
    val sha256: String,
    @SerialName("received_at") val receivedAt: String,
) {
    init {
        Rules.schemaVersion(schemaVersion)
        Rules.prefixedId("cap", captureId, "capture_id")
        Rules.prefixedId("src", sourceId, "source_id")
        Rules.digest(sha256, "sha256")
        Rules.timestamp(receivedAt, "received_at")
    }
}
