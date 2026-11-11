package io.github.junhcha.weaver.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Row
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import io.github.junhcha.weaver.review.reviewTitle
import io.github.junhcha.weaver.review.sourceLabel
import io.github.junhcha.weaver.review.reviewLabel
import io.github.junhcha.weaver.schema.ReviewPackage
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
import kotlinx.coroutines.launch
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.courier.CourierRuntime
import io.github.junhcha.weaver.inbox.InboxDatabase
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.review.ReviewRecord
import io.github.junhcha.weaver.review.ReviewRuntime
import io.github.junhcha.weaver.review.ReviewStatus
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** Review entry shared by home and notifications. */
@Composable
fun InboxPanel(openedFromNotification: Boolean = false) {
    val context = LocalContext.current.applicationContext
    val scope = rememberCoroutineScope()
    val changes by InboxRuntime.changes.collectAsState()
    var reviews by remember { mutableStateOf(emptyList<InboxDatabase.ReviewItem>()) }
    var packages by remember { mutableStateOf(emptyMap<String, ReviewPackage?>()) }
    var selected by rememberSaveable { mutableStateOf<String?>(null) }
    var alerts by remember { mutableStateOf(emptyList<InboxDatabase.AlertItem>()) }
    var records by remember { mutableStateOf(emptyList<ReviewRecord>()) }
    LaunchedEffect(changes) {
        alerts = withContext(Dispatchers.IO) { InboxRuntime.alerts(context) }
        records = withContext(Dispatchers.IO) { ReviewRuntime.records(context) }
        withContext(Dispatchers.IO) { InboxRuntime.reviews(context) }.let { items ->
            reviews = items
            packages = withContext(Dispatchers.IO) {
                items.associate { item -> item.packageId to runCatching {
                    ReviewRuntime.open(context, item.packageId)
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
                val p = packages[item.packageId]
                val record = records.firstOrNull { it.verdict.packageId == item.packageId && it.verdict.revision == item.revision }
                OutlinedCard(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            Surface(color = when (p?.sourceLabel()) {
                                "통화" -> MaterialTheme.colorScheme.tertiaryContainer
                                "카카오톡" -> MaterialTheme.colorScheme.primaryContainer
                                "음성 메모" -> MaterialTheme.colorScheme.surfaceVariant
                                else -> MaterialTheme.colorScheme.secondaryContainer
                            }, shape = MaterialTheme.shapes.small) {
                                Text(p?.sourceLabel() ?: "문서", Modifier.padding(horizontal = 8.dp, vertical = 4.dp))
                            }
                            Surface(color = if ((p == null && item.packageId in packages) || record?.status in setOf(ReviewStatus.ERROR, ReviewStatus.CONFLICT))
                                MaterialTheme.colorScheme.errorContainer else MaterialTheme.colorScheme.primaryContainer,
                                shape = MaterialTheme.shapes.small) {
                                Text(if (p == null && item.packageId in packages) "문서 확인 필요" else record?.status?.reviewLabel() ?: if (item.revised) "피드백 반영 · 다시 확인" else "검토 필요",
                                    Modifier.padding(horizontal = 8.dp, vertical = 4.dp))
                            }
                        }
                        Text(p?.reviewTitle() ?: item.summary, style = MaterialTheme.typography.titleMedium)
                        Text(p?.questions?.size?.let { if (it == 0) "요약을 확인해 주세요" else "확인할 질문 ${it}개" } ?: if (item.packageId in packages) "문서를 읽지 못했습니다. 동기화 새로고침 후 다시 확인해 주세요." else "문서를 준비하고 있습니다")
                        OutlinedButton(onClick = { selected = item.packageId }) { Text("문서 검토") }
                    }
                }
                if (selected == item.packageId) {
                    BackHandler { selected = null }
                    ReviewPanel(item.packageId, onClose = { selected = null })
                }
            }
            alerts.forEach { alert ->
                Surface(color = MaterialTheme.colorScheme.errorContainer, shape = MaterialTheme.shapes.small) {
                    Column(Modifier.padding(12.dp)) {
                        Text("확인할 일정 변경", style = MaterialTheme.typography.titleSmall)
                        Text(alert.message)
                        OutlinedButton(onClick = {
                            scope.launch(Dispatchers.IO) { InboxRuntime.dismissAlert(context, alert.alertId) }
                        }) { Text("확인했어요") }
                    }
                }
            }
            records.filter { it.status == ReviewStatus.ERROR && reviews.none { item -> item.packageId == it.verdict.packageId } }.forEach { record ->
                Text(record.status.reviewLabel())
                OutlinedButton(onClick = { ReviewRuntime.retry(context) }) { Text("다시 보내기") }
            }
        }
    }
}
