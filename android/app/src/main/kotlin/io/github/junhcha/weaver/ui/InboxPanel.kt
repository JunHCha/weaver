package io.github.junhcha.weaver.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.inbox.InboxDatabase
import io.github.junhcha.weaver.inbox.InboxRuntime
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/** Minimal review entry from notifications: the local list only. Full review is #35. */
@Composable
fun InboxPanel(openedFromNotification: Boolean = false) {
    val context = LocalContext.current.applicationContext
    val scope = rememberCoroutineScope()
    val changes by InboxRuntime.changes.collectAsState()
    var proposals by remember { mutableStateOf(emptyList<InboxDatabase.ProposalItem>()) }
    var alerts by remember { mutableStateOf(emptyList<InboxDatabase.AlertItem>()) }
    var lastScan by remember { mutableStateOf<Long?>(null) }
    LaunchedEffect(changes) {
        withContext(Dispatchers.IO) {
            Triple(InboxRuntime.proposals(context), InboxRuntime.alerts(context), InboxRuntime.lastScanAt(context))
        }.let { (p, a, scan) -> proposals = p; alerts = a; lastScan = scan }
    }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(if (openedFromNotification) "알림에서 연 검토 대기 목록" else "검토 대기 제안 ${proposals.size}건")
            if (proposals.isEmpty()) Text("검토 대기 제안 없음")
            proposals.forEach { item ->
                Text(item.summary + if (item.revised) " · 다시 짠 제안 (r${item.rev})" else "")
            }
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
