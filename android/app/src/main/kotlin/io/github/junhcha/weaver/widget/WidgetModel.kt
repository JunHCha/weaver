package io.github.junhcha.weaver.widget

import android.content.Context
import io.github.junhcha.weaver.courier.CourierRuntime
import io.github.junhcha.weaver.inbox.InboxDatabase
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.review.ReviewRecord
import io.github.junhcha.weaver.review.ReviewRuntime
import io.github.junhcha.weaver.review.ReviewStatus

/** Only the local notification summaries enter RemoteViews, never full originals or schedules. */
data class WidgetRow(val id: String, val revision: Long, val summary: String, val submitted: Boolean, val deliveryStatus: String? = null)
data class WidgetModel(
    val rows: List<WidgetRow> = emptyList(), val alertMessages: List<String> = emptyList(),
    val status: String = "동기화 대기", val failed: Boolean = false,
) {
    val pendingCount get() = rows.count { !it.submitted }
    fun visible(large: Boolean) = rows.take(if (large) 5 else 2)
    fun overflow(large: Boolean) = rows.size - visible(large).size
    fun warning(large: Boolean): String? = if (alertMessages.isEmpty()) null else
        "일정이 바뀌었을 수 있습니다 (${alertMessages.size}건)." +
            if (large) " ${alertMessages.first().replace('\n', ' ').split(Regex("[.!?。]\\s*")).first().take(90)}." else ""

    companion object {
        fun map(reviews: List<InboxDatabase.ReviewItem>, records: List<ReviewRecord>,
                alerts: List<String>, status: String): WidgetModel {
            val submitted = records.associateBy { it.verdict.packageId to it.verdict.revision }
            // Inbox is newest-arrival first; stable sorting preserves that order within each state.
            val rows = reviews.groupBy { it.packageId }.values.map { versions -> versions.maxBy { it.revision } }
                .map { WidgetRow(it.packageId, it.revision, it.summary, (it.packageId to it.revision) in submitted,
                    submitted[it.packageId to it.revision]?.let(::deliveryLabel)) }.sortedBy { it.submitted }
            return WidgetModel(rows, alerts, status)
        }
        private fun deliveryLabel(record: ReviewRecord): String = if (record.published) "전송됨" else when (record.status) {
            ReviewStatus.PENDING -> "전송 대기"
            ReviewStatus.ERROR -> "전송 오류"
            ReviewStatus.CONFLICT -> "전송 충돌"
            ReviewStatus.STALE -> "검토 보류"
            ReviewStatus.SENT, ReviewStatus.INFERRED -> "전송됨"
        }
        fun load(context: Context, now: Long = System.currentTimeMillis()): WidgetModel = try {
            // Same lock order as the scan/review path; never acquire Courier while holding Inbox.
            synchronized(CourierRuntime.lock) {
                val records = ReviewRuntime.records(context)
                synchronized(InboxRuntime.lock) {
                    InboxDatabase(context).use { db ->
                        val scan = db.lastScanAt()
                        val status = when {
                            CourierRuntime.stopped(context) -> "동기화 오류 · 앱에서 폴더를 확인하세요"
                            WidgetUpdates.scanFailed(context) -> "동기화 오류 · 이전 목록입니다"
                            scan == null -> "동기화 대기 · 아직 검사하지 않았습니다"
                            now - scan > 30 * 60_000 -> "동기화 지연 · 이전 목록입니다"
                            else -> "최근 검사 완료 · 약 15분 주기"
                        }
                        map(db.reviews(newestRevisionFirst = true), records.filter { it.tree == db.setting("tree") }, db.alerts().map { it.message }, status)
                    }
                }
            }
        } catch (_: Exception) { WidgetModel(status = "목록 읽기 오류 · 앱에서 확인하세요", failed = true) }
    }
}
