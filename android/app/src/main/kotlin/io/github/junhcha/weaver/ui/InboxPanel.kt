package io.github.junhcha.weaver.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.OutlinedCard
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.courier.CourierRuntime
import io.github.junhcha.weaver.inbox.InboxDatabase
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.review.ReviewRecord
import io.github.junhcha.weaver.review.ReviewRuntime
import io.github.junhcha.weaver.review.ReviewStatus
import io.github.junhcha.weaver.review.displayName
import io.github.junhcha.weaver.schema.Stage
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
    var reviews by remember { mutableStateOf(emptyList<InboxDatabase.ReviewItem>()) }
    var questionCounts by remember { mutableStateOf(emptyMap<String, Int?>()) }
    var selected by rememberSaveable { mutableStateOf<String?>(null) }
    var records by remember { mutableStateOf(emptyList<ReviewRecord>()) }
    var alerts by remember { mutableStateOf(emptyList<InboxDatabase.AlertItem>()) }
    var lastScan by remember { mutableStateOf<Long?>(null) }
    LaunchedEffect(changes) {
        records = withContext(Dispatchers.IO) { ReviewRuntime.records(context) }
        withContext(Dispatchers.IO) {
            Triple(InboxRuntime.reviews(context), InboxRuntime.alerts(context), InboxRuntime.lastScanAt(context))
        }.let { (items, a, scan) ->
            reviews = items; alerts = a; lastScan = scan
            questionCounts = withContext(Dispatchers.IO) {
                items.associate { item -> item.packageId to runCatching {
                    ReviewRuntime.open(context, item.packageId).questions.size
                }.getOrNull() }
            }
        }
    }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(if (openedFromNotification) "알림에서 연 검토 대기 목록" else "검토 대기 문서 ${reviews.size}건")
            OutlinedButton(onClick = { CourierRuntime.kick(context) }) { Text("동기화 새로고침") }
            if (reviews.isEmpty()) Text("검토 대기 문서 없음")
            reviews.forEach { item ->
                OutlinedCard(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text("단계: ${stageLabel(item.stage)}")
                        Text("revision ${item.revision} · 미해결 질문 ${questionCounts[item.packageId]?.let { "${it}개" } ?: if (item.packageId in questionCounts) "확인 불가" else "확인 중"}" +
                            if (item.revised) " · 피드백 반영본" else "")
                        Text(item.summary.replace('\n', ' '), maxLines = 1, overflow = TextOverflow.Ellipsis)
                        OutlinedButton(onClick = { selected = item.packageId }) { Text("문서 r${item.revision} 검토") }
                    }
                }
            }
            records.forEach { record ->
                Text("${record.verdict.packageId} r${record.verdict.revision}: ${record.status.label}")
                if (record.status in setOf(ReviewStatus.ERROR, ReviewStatus.PENDING))
                    OutlinedButton(onClick = { ReviewRuntime.retry(context) }) { Text("재시도") }
            }
            selected?.let { id -> ReviewPanel(id, onClose = { selected = null }) }
            Text("통화 정리는 갤럭시 스크립트 텍스트를 공유해 시작하세요. 홈의 Shepherd 통화 목록에서 확인할 수 있습니다.")
            Text("변경 경고 ${alerts.size}건")
            alerts.forEach { alert ->
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

private fun stageLabel(stage: String): String =
    runCatching { Stage.valueOf(stage).displayName() }.getOrDefault(stage)
