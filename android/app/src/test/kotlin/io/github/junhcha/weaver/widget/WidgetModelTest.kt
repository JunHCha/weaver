package io.github.junhcha.weaver.widget

import io.github.junhcha.weaver.inbox.InboxDatabase
import io.github.junhcha.weaver.review.ReviewRecord
import io.github.junhcha.weaver.review.ReviewStatus
import io.github.junhcha.weaver.schema.Verdict
import org.junit.Assert.*
import org.junit.Test

class WidgetModelTest {
    private val id = "prp_01ARZ3NDEKTSV4RRFFQ69G5FA0"
    private fun item(id: String = this.id, rev: Long = 1) = InboxDatabase.ProposalItem(
        id, rev, "아주 긴 한국어 제안 요약 ".repeat(20), "CALL", "2026-11-08T00:00:00Z", false, 0)
    private fun record(rev: Long, status: ReviewStatus) = ReviewRecord(
        Verdict(1, id, rev, Verdict.Kind.APPROVE, emptyList(), null, null, "2026-11-08T00:00:00Z"), "test", status)

    @Test fun latestRevisionOnlyAndSubmittedRowsFollowPendingRows() {
        val model = WidgetModel.map(listOf(item(), item("new"), item(rev = 3), item(rev = 2)),
            listOf(record(3, ReviewStatus.PENDING)), emptyList(), "fresh")
        assertEquals(1, model.pendingCount)
        assertEquals(listOf("new", id), model.rows.map { it.id })
        assertEquals(3L, model.rows.last().rev)
        assertTrue(model.rows.last().submitted)
        // A new revision needs its own verdict, even when the old rev was sent.
        assertEquals(1, WidgetModel.map(listOf(item(rev = 4)), listOf(record(3, ReviewStatus.SENT)), emptyList(), "").pendingCount)
        for (state in ReviewStatus.entries) {
            assertEquals(0, WidgetModel.map(listOf(item()), listOf(record(1, state)), emptyList(), "").pendingCount)
        }
    }
    @Test fun rowAndWarningBudgetsIncludeOverflowAndExplicitApprovalBoundary() {
        for (count in listOf(0, 1, 2, 6)) for (hasAlert in listOf(false, true)) {
            val model = WidgetModel.map((0 until count).map { item("id$it") }, emptyList(),
                if (hasAlert) listOf("일정 후보가 달라졌습니다. 다른 문장입니다.") else emptyList(), "")
            assertEquals(count, model.pendingCount)
            assertEquals(minOf(2, count), model.visible(false).size)
            assertEquals(minOf(5, count), model.visible(true).size)
            assertEquals(maxOf(0, count - 2), model.overflow(false))
            assertEquals(maxOf(0, count - 5), model.overflow(true))
            assertEquals(hasAlert, model.warning(false) != null)
            if (hasAlert) {
                assertTrue(model.warning(false)!!.contains("승인 전 · 일정이 바뀔 수 있습니다"))
                assertFalse(model.warning(false)!!.contains("일정 후보"))
                assertTrue(model.warning(true)!!.contains("일정 후보"))
                assertFalse(model.warning(true)!!.contains("다른 문장"))
            }
        }
    }
    @Test fun staleClicksResolveLatestOrMissingWithoutOfferingOldRevision() {
        assertEquals(WidgetReviewTarget.Ready(id, 1, 3), WidgetReviewTarget.resolve(id, 1) { 3 })
        assertEquals(WidgetReviewTarget.Ready(id, 3, 3), WidgetReviewTarget.resolve(id, 3) { 3 })
        val missing = WidgetReviewTarget.resolve(id, 1) { error("제안이 없습니다") }
        assertTrue((missing as WidgetReviewTarget.Unavailable).message.contains("사라졌습니다"))
        assertTrue(WidgetReviewTarget.resolve(id, 3) { 2 } is WidgetReviewTarget.Unavailable)
        assertTrue(WidgetReviewTarget.resolve(id, 3) { throw SecurityException() } is WidgetReviewTarget.Unavailable)
        assertTrue(WidgetReviewTarget.resolve("invalid", 1) { error("must not read") } is WidgetReviewTarget.Unavailable)
        assertTrue(WidgetReviewTarget.resolve(id, 0) { error("must not read") } is WidgetReviewTarget.Unavailable)
    }
}
