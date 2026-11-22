package io.github.junhcha.weaver.transport

import io.github.junhcha.weaver.schema.Alert
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement

@Serializable
data class AlertEnvelope(val items: List<JsonElement>, @SerialName("next_cursor") val nextCursor: String?)

@Serializable
data class AlertAck(@SerialName("alert_id") val alertId: String, val state: String) {
    init { require(state == "received") }
}

/** Only this explicit, versioned capability enables HTTP Alerts; review transport is independent. */
@Serializable
data class AlertCapabilities(
    @SerialName("schema_versions") val schemaVersions: Map<String, Int> = emptyMap(),
    val transports: Map<String, String> = emptyMap(),
    val features: Map<String, Boolean> = emptyMap(),
) {
    val http: Boolean get() = schemaVersions["alert"] == 2 && transports["alert"] == "http" && features["alerts"] == true
}

@Serializable
data class AlertError(val code: String, val retryable: Boolean)

/** Capabilities share an extensible object with unrelated features; tolerate their keys only here. */
internal val alertWireJson = Json { ignoreUnknownKeys = true }

/** All methods return only after a durable local commit. Never infer receipt from adapter file absence. */
interface AlertLedger {
    fun receive(alert: Alert)
    fun pending(): List<String>
    fun acknowledged(alertId: String)
}
