package io.github.junhcha.weaver.schema

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
enum class PipelineLocation {
    @SerialName("spinner") SPINNER,
    @SerialName("loom") LOOM,
    @SerialName("user") USER,
    @SerialName("fabric") FABRIC,
    @SerialName("wiki") WIKI,
}

@Serializable
enum class PipelineStatus {
    @SerialName("working") WORKING,
    @SerialName("review") REVIEW,
    @SerialName("applying") APPLYING,
    @SerialName("apply_stopped") APPLY_STOPPED,
    @SerialName("done") DONE,
    @SerialName("rejected") REJECTED,
}

@Serializable
data class PipelineThread(@SerialName("thread_id") val threadId: String, val kind: String) {
    init {
        Rules.nonEmpty(threadId, "thread_id")
        require(kind in setOf("요청", "결정", "일정", "기록", "태그정정", "거절"))
    }
}

@Serializable
data class PipelinePage(
    val path: String,
    @SerialName("updated_at") val updatedAt: String,
    val sha256: String,
) {
    init {
        require(
            path.isNotBlank() &&
                !path.startsWith('/') &&
                '\\' !in path &&
                path.none { it.isISOControl() } &&
                path.endsWith(".md") &&
                path.split('/').all {
                    it.isNotBlank() && !it.startsWith('.') && it !in setOf(".", "..")
                }
        ) {
            "unsafe wiki path"
        }
        Rules.timestamp(updatedAt, "updated_at")
        Rules.digest(sha256, "sha256")
    }
}

/** Additive v2 observation, not a review package or an instruction to execute work. */
@Serializable
data class PipelineItem(
    @SerialName("schema_version") val schemaVersion: Int,
    val revision: Long,
    @SerialName("capture_id") val captureId: String,
    @SerialName("source_id") val sourceId: String,
    @SerialName("package_id") val packageId: String?,
    @SerialName("package_revision") val packageRevision: Long?,
    @SerialName("source_type") val sourceType: String,
    val title: String,
    val location: PipelineLocation,
    val status: PipelineStatus,
    @SerialName("stage_times") val stageTimes: Map<Stage, String>,
    val history: List<PipelineLocation>,
    val threads: List<PipelineThread>,
    val pages: List<PipelinePage>,
    val digest: PipelinePage?,
    @SerialName("apply_failure_limit") val applyFailureLimit: Long,
    @SerialName("updated_at") val updatedAt: String,
) {
    init {
        Rules.schemaVersion(schemaVersion, 2)
        Rules.revision(revision)
        Rules.revision(applyFailureLimit)
        require(sourceType in setOf("call", "voice_memo", "kakao", "share_text"))
        Rules.prefixedId("cap", captureId, "capture_id")
        Rules.prefixedId("src", sourceId, "source_id")
        packageId?.let { Rules.nonEmpty(it, "package_id") }
        packageRevision?.let(Rules::revision)
        stageTimes.values.forEach { Rules.timestamp(it, "stage_times") }
        Rules.timestamp(updatedAt, "updated_at")
    }
}
