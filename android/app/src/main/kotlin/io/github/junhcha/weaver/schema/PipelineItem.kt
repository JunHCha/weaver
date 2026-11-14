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
data class PipelineThread(@SerialName("thread_id") val threadId: String, val kind: String, val content: String = "") {
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

@Serializable
data class PipelineWikiChange(val field: String, val before: String?, val after: String?) {
    init { require(field in setOf("body", "deadline", "schedule", "assignees", "status")) }
}

@Serializable
data class PipelineWikiEdit(
    val path: String,
    @SerialName("base_rev") val baseRev: String,
    @SerialName("new_rev") val newRev: String?,
    @SerialName("detected_at") val detectedAt: String?,
    @SerialName("ready_at") val readyAt: String?,
    val state: String,
    val changes: List<PipelineWikiChange>,
) {
    init {
        PipelinePage(path, "2000-01-01T00:00:00Z", "0".repeat(64))
        Rules.nonEmpty(baseRev, "base_rev")
        newRev?.let { Rules.nonEmpty(it, "new_rev") }
        detectedAt?.let { Rules.timestamp(it, "detected_at") }
        readyAt?.let { Rules.timestamp(it, "ready_at") }
        require(state in setOf("settling", "recorded", "published", "unchanged"))
    }
}

/** Additive v2 observation, not a review package or an instruction to execute work. */
@Serializable
data class PipelineItem(
    @SerialName("schema_version") val schemaVersion: Int,
    val revision: Long,
    @SerialName("capture_id") val captureId: String?,
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
    @SerialName("wiki_edit") val wikiEdit: PipelineWikiEdit? = null,
) {
    init {
        Rules.schemaVersion(schemaVersion, 2)
        Rules.revision(revision)
        Rules.revision(applyFailureLimit)
        require(sourceType in setOf("call", "voice_memo", "kakao", "share_text", "wiki_edit"))
        if (sourceType == "wiki_edit") {
            require(captureId == null && wikiEdit != null && packageId == null && packageRevision == null)
            require(location in setOf(PipelineLocation.WIKI, PipelineLocation.FABRIC))
            require(status in setOf(PipelineStatus.WORKING, PipelineStatus.DONE))
            require(history.firstOrNull() == PipelineLocation.WIKI)
            require(history.all { it in setOf(PipelineLocation.WIKI, PipelineLocation.FABRIC) })
            require(stageTimes.keys.all { it in setOf(Stage.APPLY, Stage.UPDATE_WIKI) })
        } else {
            require(wikiEdit == null)
            Rules.prefixedId("cap", requireNotNull(captureId), "capture_id")
        }
        Rules.prefixedId("src", sourceId, "source_id")
        packageId?.let { Rules.nonEmpty(it, "package_id") }
        packageRevision?.let(Rules::revision)
        stageTimes.values.forEach { Rules.timestamp(it, "stage_times") }
        Rules.timestamp(updatedAt, "updated_at")
    }
}
