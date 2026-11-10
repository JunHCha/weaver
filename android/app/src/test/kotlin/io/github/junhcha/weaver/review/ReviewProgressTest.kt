package io.github.junhcha.weaver.review

import io.github.junhcha.weaver.schema.v2.Stage
import org.junit.Assert.assertEquals
import org.junit.Test

class ReviewProgressTest {
    @Test fun stagesFollowServerStageAndPreserveTimes() {
        val stages = reviewProgress(Stage.REVIEW, mapOf(Stage.SEND to "2026-01-01T00:00:00Z"), null)
        assertEquals(Stage.entries, stages.steps.map { it.stage })
        assertEquals(StepStatus.DONE, stages.steps.first().status)
        assertEquals(StepStatus.CURRENT, stages.steps[4].status)
        assertEquals(StepStatus.UPCOMING, stages.steps[5].status)
        assertEquals("2026-01-01T00:00:00Z", stages.steps[1].time)
        assertEquals(null, stages.response)
    }

    @Test fun responseDistinguishesDeliveryAndInferredApplication() {
        assertEquals("전송 대기", reviewProgress(Stage.REVIEW, emptyMap(), ReviewStatus.PENDING).response)
        assertEquals("전송됨 · 서버 반영 대기", reviewProgress(Stage.REVIEW, emptyMap(), ReviewStatus.SENT).response)
        assertEquals("서버 반영 추정", reviewProgress(Stage.REVIEW, emptyMap(), ReviewStatus.INFERRED).response)
        assertEquals("서버 반영", reviewProgress(Stage.APPLY, emptyMap(), ReviewStatus.SENT).response)
        assertEquals(ReviewStatus.ERROR.label, reviewProgress(Stage.REVIEW, emptyMap(), ReviewStatus.ERROR).response)
        assertEquals(ReviewStatus.CONFLICT.label, reviewProgress(Stage.APPLY, emptyMap(), ReviewStatus.CONFLICT).response)
        assertEquals(ReviewStatus.STALE.label, reviewProgress(Stage.UPDATE_WIKI, emptyMap(), ReviewStatus.STALE).response)
    }
}
