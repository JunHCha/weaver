package io.github.junhcha.weaver.review

import io.github.junhcha.weaver.schema.WeaverCodec
import io.github.junhcha.weaver.schema.*
import org.junit.Assert.*
import org.junit.Test

class ReviewDraftTest {
    private val p = ReviewPackage(2, "pkg_0123456789abcdef", "src_01ARZ3NDEKTSV4RRFFQ69G5FA0", 1,
        "요약\n본문", listOf(Question("질문", "이유", listOf("하나", "둘", "셋"))), Stage.REVIEW, emptyMap())

    @Test fun answersAndThreeActionsSerialize() {
        Verdict.Action.entries.forEach { action ->
            val v = ReviewDraft.verdict(p, listOf("둘"), action, "수정 본문", "피드백")
            assertEquals(v, WeaverCodec.json.decodeFromString<Verdict>(WeaverCodec.encode(v)))
            assertEquals(listOf("둘"), v.answers)
            assertEquals(if (action == Verdict.Action.EDIT) "수정 본문" else null, v.editedBody)
            assertEquals(if (action != Verdict.Action.REJECT) "피드백" else null, v.feedback)
        }
    }
    @Test fun acceptsFreeformAnswerTogetherWithFeedback() {
        val v = ReviewDraft.verdict(p, listOf("다음 주 목요일 오후로 변경"), Verdict.Action.APPROVE, p.draftBody, "시간 확인")
        assertEquals(listOf("다음 주 목요일 오후로 변경"), v.answers)
        assertEquals("시간 확인", v.feedback)
    }
    @Test fun rejectsMissingOrInvalidAnswersAndEmptyActionText() {
        assertThrows(IllegalArgumentException::class.java) { ReviewDraft.verdict(p, emptyList(), Verdict.Action.APPROVE, "", "") }
        assertThrows(IllegalArgumentException::class.java) { ReviewDraft.verdict(p, listOf(" "), Verdict.Action.APPROVE, "", "") }
        assertThrows(IllegalArgumentException::class.java) { ReviewDraft.verdict(p, listOf("하나"), Verdict.Action.EDIT, " ", "") }
    }
}
