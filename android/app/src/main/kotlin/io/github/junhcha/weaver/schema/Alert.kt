package io.github.junhcha.weaver.schema

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** PC → proposals/alert_<alert_id>.json. Mirrors schema/Alert.schema.json. */
@Serializable
data class Alert(
    @SerialName("schema_version") val schemaVersion: Int,
    @SerialName("alert_id") val alertId: String,
    @SerialName("package_id") val packageId: String,
    @SerialName("thread_id") val threadId: String,
    val message: String,
    @SerialName("created_at") val createdAt: String,
) {
    init {
        Rules.schemaVersion(schemaVersion, REVIEW_SCHEMA_VERSION)
        Rules.prefixedId("alt", alertId, "alert_id")
        Rules.nonEmpty(packageId, "package_id")
        Rules.nonEmpty(threadId, "thread_id")
        Rules.nonEmpty(message, "message")
        Rules.timestamp(createdAt, "created_at")
    }
}
