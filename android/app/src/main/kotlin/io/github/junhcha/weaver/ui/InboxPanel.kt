package io.github.junhcha.weaver.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.OutlinedCard
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.key
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.text.style.TextOverflow
import io.github.junhcha.weaver.review.ReviewDraft
import io.github.junhcha.weaver.courier.CourierRuntime
import io.github.junhcha.weaver.inbox.InboxDatabase
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.review.ReviewRecord
import io.github.junhcha.weaver.review.ReviewRuntime
import io.github.junhcha.weaver.review.ReviewStatus
import io.github.junhcha.weaver.schema.Verdict
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/** Review entry shared by home and notifications. */
@Composable
fun InboxPanel(openedFromNotification: Boolean = false) {
    val context = LocalContext.current.applicationContext
    val scope = rememberCoroutineScope()
    val changes by InboxRuntime.changes.collectAsState()
    var proposals by remember { mutableStateOf(emptyList<InboxDatabase.ProposalItem>()) }
    var questionCounts by remember { mutableStateOf(emptyMap<String, Int?>()) }
    var selected by rememberSaveable { mutableStateOf<String?>(null) }
    var records by remember { mutableStateOf(emptyList<ReviewRecord>()) }
    var alerts by remember { mutableStateOf(emptyList<InboxDatabase.AlertItem>()) }
    var lastScan by remember { mutableStateOf<Long?>(null) }
    LaunchedEffect(changes) {
        records = withContext(Dispatchers.IO) { ReviewRuntime.records(context) }
        withContext(Dispatchers.IO) {
            Triple(InboxRuntime.proposals(context), InboxRuntime.alerts(context), InboxRuntime.lastScanAt(context))
        }.let { (p, a, scan) ->
            proposals = p; alerts = a; lastScan = scan
            questionCounts = withContext(Dispatchers.IO) {
                p.associate { item -> item.proposalId to runCatching {
                    val proposal = ReviewRuntime.open(context, item.proposalId)
                    check(proposal.rev == item.rev)
                    ReviewDraft.questions(proposal).size
                }.getOrNull() }
            }
        }
    }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(if (openedFromNotification) "알림에서 연 검토 대기 목록" else "검토 대기 제안 ${proposals.size}건")
            OutlinedButton(onClick = { CourierRuntime.kick(context) }) { Text("동기화 새로고침") }
            if (proposals.isEmpty()) Text("검토 대기 제안 없음")
            proposals.forEach { item ->
                OutlinedCard(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text("출처: ${channelLabel(item.channel)} · 시각: ${capturedAtLabel(item.capturedAt)}")
                        Text("rev ${item.rev} · 미해결 질문 ${questionCounts[item.proposalId]?.let { "${it}개" } ?: if (item.proposalId in questionCounts) "확인 불가" else "확인 중"}" +
                            if (item.revised) " · 다시 짠 제안" else "")
                        Text(item.summary.replace('\n', ' '), maxLines = 1, overflow = TextOverflow.Ellipsis)
                        OutlinedButton(onClick = { selected = item.proposalId }) { Text("제안 r${item.rev} 검토") }
                    }
                }
            }
            records.forEach { record ->
                key(record.verdict.proposalId, record.verdict.rev) { CalendarPanel(record) }
                Text("${record.verdict.proposalId} r${record.verdict.rev}: ${record.status.label}")
                if (record.verdict.verdict == Verdict.Kind.REVISE) {
                    Text(if (proposals.any { it.proposalId == record.verdict.proposalId && it.rev > record.verdict.rev })
                        "새 rev 도착 · 최신 제안을 확인하세요" else "새 rev 대기")
                }
                if (record.status in setOf(ReviewStatus.ERROR,
                        ReviewStatus.PENDING)) {
                    OutlinedButton(onClick = { ReviewRuntime.retry(context) }) { Text("재시도") }
                }
            }
            selected?.let { id -> ReviewPanel(id, onClose = { selected = null }) }
            Text("통화 정리는 갤럭시 스크립트 텍스트를 공유해 시작하세요. 홈의 Shepherd 통화 목록에서 확인할 수 있습니다.")
            Text("변경 경고 ${alerts.size}건")
            alerts.forEach { alert ->
                Text("승인 전 · 일정이 바뀔 수 있음")
                Text(alert.message)
                OutlinedButton(onClick = {
                    scope.launch(Dispatchers.IO) { InboxRuntime.dismissAlert(context, alert.alertId) }
                }) { Text("경고 닫기") }
            }
            Text("검사 주기 ${InboxRuntime.INTERVAL_MINUTES}분 · 기기 절전(Doze·배터리 최적화)으로 더 늦어질 수 있습니다.")
            Text("마지막 성공 검사: " + (lastScan?.let {
                DateTimeFormatter.ofPattern("MM-dd HH:mm").format(Instant.ofEpochMilli(it).atZone(ZoneId.systemDefault()))
            } ?: "아직 없음"))
        }
    }
}

private fun channelLabel(channel: String): String = when (channel) {
    "CALL" -> "통화"
    "VOICE_MEMO" -> "음성 메모"
    "KAKAO" -> "카카오톡"
    "SHARE_TEXT" -> "공유 텍스트"
    else -> channel
}

internal fun capturedAtLabel(value: String, zone: ZoneId = ZoneId.systemDefault()): String =
    runCatching {
        DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm").format(Instant.parse(value).atZone(zone))
    }.getOrDefault(value)
