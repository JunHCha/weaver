package io.github.junhcha.weaver.review

import io.github.junhcha.weaver.schema.WeaverCodec
import io.github.junhcha.weaver.schema.v2.*
import org.junit.Assert.*
import org.junit.Test

class ReviewV2DraftTest {
    private val p = ReviewPackage(2, "prp_01ARZ3NDEKTSV4RRFFQ69G5FA0", "src_01ARZ3NDEKTSV4RRFFQ69G5FA0", 1,
        "요약\n본문", listOf(Question("질문", "이유", listOf("하나", "둘", "셋"))), Stage.REVIEW, emptyMap())

    @Test fun answersAndFourActionsSerialize() {
        Verdict.Action.entries.forEach { action ->
            val v = ReviewV2Draft.verdict(p, listOf("둘"), action, "수정 본문", "피드백")
            assertEquals(v, WeaverCodec.json.decodeFromString<Verdict>(WeaverCodec.encode(v)))
            assertEquals(listOf("둘"), v.answers)
            assertEquals(if (action == Verdict.Action.EDIT) "수정 본문" else null, v.editedBody)
            assertEquals(if (action == Verdict.Action.FEEDBACK) "피드백" else null, v.feedback)
        }
    }
    @Test fun rejectsMissingOrInvalidAnswersAndEmptyActionText() {
        assertThrows(IllegalArgumentException::class.java) { ReviewV2Draft.verdict(p, emptyList(), Verdict.Action.APPROVE, "", "") }
        assertThrows(IllegalArgumentException::class.java) { ReviewV2Draft.verdict(p, listOf("다른 답"), Verdict.Action.APPROVE, "", "") }
        assertThrows(IllegalArgumentException::class.java) { ReviewV2Draft.verdict(p, listOf("하나"), Verdict.Action.EDIT, " ", "") }
        assertThrows(IllegalArgumentException::class.java) { ReviewV2Draft.verdict(p, listOf("하나"), Verdict.Action.FEEDBACK, "", " ") }
    }
}
