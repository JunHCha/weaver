@file:OptIn(kotlinx.serialization.ExperimentalSerializationApi::class)

package io.github.junhcha.weaver.schema

import kotlinx.serialization.EncodeDefault
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
enum class Stage {
    @SerialName("수집") COLLECT, @SerialName("전송") SEND, @SerialName("정리") ORGANIZE,
    @SerialName("질문") QUESTION, @SerialName("검토") REVIEW, @SerialName("반영") APPLY,
    @SerialName("wiki갱신") UPDATE_WIKI,
}

@Serializable
data class Question(val question: String, val rationale: String, val choices: List<String>,
    @EncodeDefault(EncodeDefault.Mode.NEVER) @SerialName("source_excerpt") val sourceExcerpt: String = "",
) {
    init {
        Rules.nonEmpty(question, "question")
        Rules.nonEmpty(rationale, "rationale")
        require(choices.size == 3)
        choices.forEach { Rules.nonEmpty(it, "choice") }
    }
}

@Serializable
data class ReviewPackage(
    @SerialName("schema_version") val schemaVersion: Int,
    @SerialName("package_id") val packageId: String,
    @SerialName("source_id") val sourceId: String,
    val revision: Long,
    @SerialName("draft_body") val draftBody: String,
    val questions: List<Question>,
    val stage: Stage,
    @SerialName("stage_times") val stageTimes: Map<Stage, String>,
    @EncodeDefault(EncodeDefault.Mode.NEVER) @SerialName("source_text") val sourceText: String = "",
    @EncodeDefault(EncodeDefault.Mode.NEVER) @SerialName("source_type") val sourceType: String = "",
    @EncodeDefault(EncodeDefault.Mode.NEVER) val title: String = "",
) {
    init {
        Rules.schemaVersion(schemaVersion, REVIEW_SCHEMA_VERSION)
        Rules.nonEmpty(packageId, "package_id")
        Rules.prefixedId("src", sourceId, "source_id")
        Rules.revision(revision)
        Rules.nonEmpty(draftBody, "draft_body")
        require(questions.size <= 3)
        stageTimes.values.forEach { Rules.timestamp(it, "stage_times") }
    }
}
