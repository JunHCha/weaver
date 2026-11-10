package io.github.junhcha.weaver.schema.v2

import io.github.junhcha.weaver.schema.Rules
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class Verdict(
    @SerialName("schema_version") val schemaVersion: Int,
    @SerialName("package_id") val packageId: String,
    val revision: Long,
    val action: Action,
    val answers: List<String>,
    @SerialName("edited_body") val editedBody: String?,
    val feedback: String?,
) {
    init {
        require(schemaVersion == 2)
        Rules.nonEmpty(packageId, "package_id")
        Rules.revision(revision)
        answers.forEach { Rules.nonEmpty(it, "answer") }
        editedBody?.let { Rules.nonEmpty(it, "edited_body") }
        feedback?.let { Rules.nonEmpty(it, "feedback") }
        require((editedBody != null) == (action == Action.EDIT))
        require((feedback != null) == (action == Action.FEEDBACK))
    }

    @Serializable
    enum class Action {
        @SerialName("approve") APPROVE, @SerialName("edit") EDIT,
        @SerialName("feedback") FEEDBACK, @SerialName("reject") REJECT,
    }
}
