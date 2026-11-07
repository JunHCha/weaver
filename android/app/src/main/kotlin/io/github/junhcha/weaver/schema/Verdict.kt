package io.github.junhcha.weaver.schema

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

/**
 * Review result → decisions/<proposal_id>.r<rev>.json. Mirrors schema/Verdict.schema.json,
 * including its four oneOf branches. approve/edit record facts only; they never resolve
 * requests or authorize calendar execution.
 */
@Serializable
data class Verdict(
    @SerialName("schema_version") val schemaVersion: Int,
    @SerialName("proposal_id") val proposalId: String,
    val rev: Int,
    val verdict: Kind,
    val edits: List<Edit>,
    @SerialName("reject_reason") val rejectReason: RejectReason?,
    val instruction: String?,
    @SerialName("reviewed_at") val reviewedAt: String,
) {
    init {
        Rules.schemaVersion(schemaVersion)
        Rules.prefixedId("prp", proposalId, "proposal_id")
        require(rev >= 1) { "rev must be >= 1" }
        Rules.timestamp(reviewedAt, "reviewed_at")
        instruction?.let { Rules.nonEmpty(it, "instruction") }
        when (verdict) {
            Kind.APPROVE -> require(edits.isEmpty() && rejectReason == null && instruction == null) {
                "approve requires edits=[], reject_reason=null, instruction=null"
            }
            Kind.EDIT -> require(edits.isNotEmpty() && rejectReason == null && instruction == null) {
                "edit requires nonempty edits, reject_reason=null, instruction=null"
            }
            Kind.REVISE -> require(edits.isEmpty() && rejectReason == null && instruction != null) {
                "revise requires edits=[], reject_reason=null and an instruction"
            }
            Kind.REJECT -> require(edits.isEmpty() && rejectReason != null && instruction == null) {
                "reject requires edits=[], a reject_reason and instruction=null"
            }
        }
    }

    @Serializable
    enum class Kind {
        @SerialName("approve") APPROVE,
        @SerialName("edit") EDIT,
        @SerialName("revise") REVISE,
        @SerialName("reject") REJECT,
    }

    @Serializable
    enum class RejectReason {
        @SerialName("wrong_person") WRONG_PERSON,
        @SerialName("wrong_content") WRONG_CONTENT,
        @SerialName("duplicate") DUPLICATE,
        @SerialName("not_useful") NOT_USEFUL,
        @SerialName("other") OTHER,
    }

    /**
     * RFC 6901 pointer edit. Syntax only: the future VerdictHandler must restrict editable
     * fields (never blocked_by_request, request or resolves) and revalidate the Proposal.
     */
    @Serializable
    data class Edit(val path: String, val value: JsonElement) {
        init {
            require(POINTER.matches(path)) { "edit path must be an RFC 6901 pointer" }
        }

        private companion object {
            val POINTER = Regex("(?:/(?:[^~/]|~[01])*)*")
        }
    }
}
