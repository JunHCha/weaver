package io.github.junhcha.weaver.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SwipeToDismissBox
import androidx.compose.material3.SwipeToDismissBoxValue
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberSwipeToDismissBoxState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.CustomAccessibilityAction
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.customActions
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.inbox.InboxDatabase
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.phone.SeoulZone
import io.github.junhcha.weaver.review.PendingReject
import io.github.junhcha.weaver.review.ReviewRecord
import io.github.junhcha.weaver.review.ReviewRuntime
import io.github.junhcha.weaver.review.ReviewStatus
import io.github.junhcha.weaver.review.reviewTitle
import io.github.junhcha.weaver.review.sourceLabel
import io.github.junhcha.weaver.schema.ReviewPackage
import io.github.junhcha.weaver.schema.Stage
import java.time.Instant
import java.time.ZoneId
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

data class ApprovalRow(
    val packageId: String, val revision: Long, val title: String, val meta: String,
    val badge: String?, val readable: Boolean, val pendingReject: PendingReject?,
)

/** Rows still waiting on the human. A package with a sent or applied record is done; only failed sends stay. */
fun approvalRows(
    reviews: List<InboxDatabase.ReviewItem>,
    packages: Map<String, ReviewPackage?>,
    records: List<ReviewRecord>,
    pending: List<PendingReject>,
    counterparts: Map<String, String>,
    zone: ZoneId,
): List<ApprovalRow> = reviews.mapNotNull { item ->
    val p = packages[item.packageId]
    val record = records.firstOrNull { it.verdict.packageId == item.packageId && it.verdict.revision == item.revision }
    if (record != null && record.status != ReviewStatus.ERROR && record.status != ReviewStatus.CONFLICT) return@mapNotNull null
    val collected = p?.stageTimes?.get(Stage.COLLECT)?.let { runCatching { Instant.parse(it).atZone(zone).toLocalDate().shortDate() }.getOrNull() }
    ApprovalRow(
        packageId = item.packageId, revision = item.revision,
        title = p?.reviewTitle() ?: item.summary,
        meta = listOfNotNull(counterparts[item.packageId], p?.sourceLabel(), collected).joinToString(" · "),
        badge = when {
            p == null -> "문서 확인 필요"
            item.revised -> "다시 확인"
            record != null -> "전송 오류"
            else -> null
        },
        readable = p != null,
        pendingReject = pending.firstOrNull { it.packageId == item.packageId && it.revision == item.revision },
    )
}

/** Top of the "지금" tab: tap opens the review, swiping left rejects with a five second undo. */
@Composable
fun ApprovalSection(onReview: (String) -> Unit, counterparts: Map<String, String> = emptyMap(), zone: ZoneId = SeoulZone) {
    val context = LocalContext.current.applicationContext
    val scope = rememberCoroutineScope()
    val changes by InboxRuntime.changes.collectAsState()
    var reviews by remember { mutableStateOf(emptyList<InboxDatabase.ReviewItem>()) }
    var packages by remember { mutableStateOf(emptyMap<String, ReviewPackage?>()) }
    var records by remember { mutableStateOf(emptyList<ReviewRecord>()) }
    var pending by remember { mutableStateOf(emptyList<PendingReject>()) }
    LaunchedEffect(changes) {
        records = withContext(Dispatchers.IO) { ReviewRuntime.records(context) }
        pending = withContext(Dispatchers.IO) { ReviewRuntime.pendingRejects(context) }
        val items = withContext(Dispatchers.IO) { InboxRuntime.reviews(context) }
        packages = withContext(Dispatchers.IO) {
            items.associate { item -> item.packageId to runCatching { ReviewRuntime.open(context, item.packageId) }.getOrNull() }
        }
        reviews = items
    }
    // Sends each reject once its undo window has passed; undoing or reloading cancels the wait.
    LaunchedEffect(pending) {
        pending.forEach { entry ->
            launch {
                delay((entry.fireAt - System.currentTimeMillis()).coerceAtLeast(0))
                withContext(Dispatchers.IO) { ReviewRuntime.flushDueRejects(context, maxOf(System.currentTimeMillis(), entry.fireAt)) }
            }
        }
    }
    val rows = approvalRows(reviews, packages, records, pending, counterparts, zone)
    val waiting = rows.filter { it.pendingReject == null }
    val rejected = rows.filter { it.pendingReject != null }
    if (rows.isEmpty()) return
    fun reject(row: ApprovalRow) {
        val p = packages[row.packageId] ?: return
        scope.launch(Dispatchers.IO) { ReviewRuntime.scheduleReject(context, p) }
    }
    Column(Modifier.fillMaxWidth().testTag("approval-section"), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        if (waiting.isNotEmpty()) SectionHeading("승인 대기 ${waiting.size}")
        waiting.forEach { row -> key(row.packageId) { ApprovalRowView(row, onReview = { onReview(row.packageId) }, onReject = { reject(row) }) } }
        rejected.forEach { row ->
            key(row.packageId) {
                var expired by remember { mutableStateOf(false) }
                val fireAt = row.pendingReject?.fireAt ?: 0L
                LaunchedEffect(fireAt) {
                    delay((fireAt - System.currentTimeMillis()).coerceAtLeast(0))
                    expired = true
                }
                RejectToast(row.title, undoable = !expired) { scope.launch(Dispatchers.IO) { ReviewRuntime.cancelReject(context, row.packageId) } }
            }
        }
    }
}

@Composable
private fun ApprovalRowView(row: ApprovalRow, onReview: () -> Unit, onReject: () -> Unit) {
    // The state outlives recompositions, so it must call the latest row and callback, not the first composed ones.
    val currentRow by rememberUpdatedState(row)
    val currentReject by rememberUpdatedState(onReject)
    val state = rememberSwipeToDismissBoxState(confirmValueChange = { value ->
        if (value == SwipeToDismissBoxValue.EndToStart && currentRow.readable) currentReject()
        false // the row leaves once the pending entry loads, so the state always snaps back
    })
    SwipeToDismissBox(
        state, enableDismissFromStartToEnd = false, enableDismissFromEndToStart = row.readable,
        backgroundContent = {
            Box(Modifier.fillMaxWidth().background(WeaverTokens.line, RoundedCornerShape(WeaverTokens.number("radius.card").dp)).padding(end = 16.dp), contentAlignment = Alignment.CenterEnd) {
                Text("거절", style = MaterialTheme.typography.labelLarge, color = WeaverTokens.muted)
            }
        },
    ) {
        KindCard(
            Modifier.testTag("approval-row-${row.packageId}").defaultMinSize(minHeight = 48.dp)
                .clickable(role = Role.Button, onClick = onReview)
                .semantics { if (row.readable) customActions = listOf(CustomAccessibilityAction("거절") { onReject(); true }) },
        ) {
            Column(Modifier.padding(horizontal = 12.dp, vertical = 8.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                Text(row.title, style = MaterialTheme.typography.titleSmall, maxLines = 2, overflow = TextOverflow.Ellipsis)
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(row.meta, Modifier.weight(1f), style = MaterialTheme.typography.labelSmall, color = WeaverTokens.muted, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    row.badge?.let {
                        Text(it, Modifier.background(WeaverTokens.selected, RoundedCornerShape(6.dp)).padding(horizontal = 6.dp, vertical = 2.dp),
                            style = MaterialTheme.typography.labelSmall, color = WeaverTokens.indigo)
                    }
                }
            }
        }
    }
}

@Composable
private fun RejectToast(title: String, undoable: Boolean, onUndo: () -> Unit) {
    Surface(Modifier.semantics { liveRegion = LiveRegionMode.Polite }, color = WeaverTokens.ink, shape = RoundedCornerShape(WeaverTokens.number("radius.card").dp)) {
        Row(Modifier.padding(start = 12.dp), verticalAlignment = Alignment.CenterVertically) {
            Text("‘$title’ 거절함 · Fabric·Wiki에 반영 안 함", Modifier.weight(1f), style = MaterialTheme.typography.labelMedium, color = WeaverTokens.surface)
            if (undoable) TextButton(onClick = onUndo, Modifier.defaultMinSize(minHeight = 48.dp)) { Text("되돌리기", color = WeaverTokens.surface) }
        }
    }
}
