package io.github.junhcha.weaver.schema.v2

import io.github.junhcha.weaver.schema.Rules
import java.time.OffsetDateTime
import java.time.ZoneId
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** A schedule published from an approved summary and current Fabric state. */
@Serializable
data class ApprovedSchedule(
    @SerialName("schema_version") val schemaVersion: Int,
    @SerialName("package_id") val packageId: String,
    @SerialName("thread_id") val threadId: String,
    val title: String,
    val start: String,
    val end: String?,
    val timezone: String,
    @SerialName("blocked_by") val blockedBy: String?,
    @SerialName("summary_id") val summaryId: String,
) {
    init {
        require(schemaVersion == 2)
        Rules.nonEmpty(packageId, "package_id")
        Rules.nonEmpty(threadId, "thread_id")
        Rules.nonEmpty(title, "title")
        Rules.timestamp(start, "start")
        end?.let { Rules.timestamp(it, "end"); require(OffsetDateTime.parse(it).toInstant() > OffsetDateTime.parse(start).toInstant()) }
        ZoneId.of(timezone)
        blockedBy?.let { Rules.nonEmpty(it, "blocked_by") }
        Rules.digest(summaryId, "summary_id")
    }
}
