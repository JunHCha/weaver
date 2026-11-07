package io.github.junhcha.weaver.schema

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** PC → proposals/alert_<alert_id>.json. Mirrors schema/Alert.schema.json. */
@Serializable
data class Alert(
    @SerialName("schema_version") val schemaVersion: Int,
    @SerialName("alert_id") val alertId: String,
    @SerialName("proposal_id") val proposalId: String,
    @SerialName("schedule_ref") val scheduleRef: String,
    val message: String,
    @SerialName("created_at") val createdAt: String,
) {
    init {
        Rules.schemaVersion(schemaVersion)
        Rules.prefixedId("alt", alertId, "alert_id")
        Rules.prefixedId("prp", proposalId, "proposal_id")
        Rules.ref(scheduleRef, "schedule_ref")
        Rules.nonEmpty(message, "message")
        Rules.timestamp(createdAt, "created_at")
    }
}
