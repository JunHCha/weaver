package io.github.junhcha.weaver.ui

import android.app.NotificationManager
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.core.app.NotificationManagerCompat
import io.github.junhcha.weaver.shepherd.CallRecord
import io.github.junhcha.weaver.shepherd.CallStatus
import io.github.junhcha.weaver.shepherd.ShepherdRuntime
import io.github.junhcha.weaver.shepherd.autoshare.AutoError
import io.github.junhcha.weaver.shepherd.autoshare.AutoShareOutcome
import io.github.junhcha.weaver.shepherd.autoshare.AutoShareRuntime
import io.github.junhcha.weaver.shepherd.autoshare.AutoShareStatus
import io.github.junhcha.weaver.shepherd.autoshare.AutoStep
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

@Composable
fun ShepherdPanel(showPendingCalls: Boolean = false) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var activated by remember { mutableStateOf(false) }
    var calls by remember { mutableStateOf(emptyList<CallRecord>()) }
    var auto by remember { mutableStateOf(AutoShareStatus(false, false, null)) }
    var onlyPending by remember(showPendingCalls) { mutableStateOf(showPendingCalls) }
    LaunchedEffect(Unit) {
        while (true) {
            val snapshot = withContext(Dispatchers.IO) {
                Triple(ShepherdRuntime.isActivated(context), ShepherdRuntime.calls(context), AutoShareRuntime.status(context))
            }
            activated = snapshot.first
            calls = snapshot.second
            auto = snapshot.third
            delay(2_000)
        }
    }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(if (onlyPending) "스크립트 미공유 통화" else "Shepherd 통화 목록")
            if (!activated) {
                Text("활성화 이후 통화만 감지합니다. 과거 통화 가져오기는 별도 기능입니다.")
                OutlinedButton(onClick = {
                    scope.launch {
                        withContext(Dispatchers.IO) { ShepherdRuntime.activate(context) }
                        activated = true
                    }
                }) { Text("Shepherd 활성화") }
            }
            if (activated && !Feature.SHEPHERD.isGranted(context)) {
                Text("오디오 읽기 권한이 없어 감지가 중단되었습니다. 아래 Shepherd 권한을 허용하거나 앱 설정에서 다시 허용하세요.")
            }
            val notifications = context.getSystemService(NotificationManager::class.java)
            if (!Feature.NOTIFICATIONS.isGranted(context) || !NotificationManagerCompat.from(context).areNotificationsEnabled() ||
                notifications.getNotificationChannel("shepherd-calls")?.importance == NotificationManager.IMPORTANCE_NONE) {
                Text("미처리 통화 알림이 꺼져 있습니다. 아래 알림 권한 또는 앱 설정에서 알림을 허용하세요.")
            }
            OutlinedButton(onClick = { onlyPending = !onlyPending }) {
                Text(if (onlyPending) "전체 통화 보기" else "미공유 통화만 보기")
            }
            val visible = calls.filter { !onlyPending || it.status == CallStatus.UNSHARED }
            if (visible.isEmpty()) Text("통화 없음")
            visible.forEach { call ->
                Text("${call.counterpart ?: "상대 미상"} · ${call.occurredAt ?: "시각 미상"}")
                Text(when (call.status) {
                    CallStatus.UNSHARED -> "스크립트 미공유"
                    CallStatus.PROCESSED -> "처리됨"
                    CallStatus.REMOVED -> "녹음 삭제됨"
                    CallStatus.DISMISSED -> "처리 안 함"
                })
                if (call.status == CallStatus.UNSHARED) {
                    OutlinedButton(onClick = {
                        scope.launch {
                            calls = withContext(Dispatchers.IO) {
                                ShepherdRuntime.dismiss(context, call.id)
                                ShepherdRuntime.calls(context)
                            }
                        }
                    }) { Text("처리 안 함") }
                }
            }
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Text("통화 스크립트 자동 공유 (접근성 서비스)", Modifier.weight(1f))
                Switch(checked = auto.enabled, onCheckedChange = { on ->
                    auto = auto.copy(enabled = on)
                    scope.launch(Dispatchers.IO) { AutoShareRuntime.setEnabled(context, on) }
                })
            }
            Text("켜면 새 통화 녹음의 스크립트를 삼성 녹음 앱에서 Weaver로 자동 공유합니다. 기본값은 꺼짐입니다. 실행 중 화면이 잠시 녹음 앱으로 바뀌며, 화면을 만지면 즉시 중단합니다.")
            if (auto.enabled && !auto.serviceOn) {
                Text("시스템 접근성 설정에서 'Weaver 통화 스크립트 자동 공유'를 켜야 동작합니다. 설치 파일로 설치한 경우 앱 정보에서 '제한된 설정 허용'이 먼저 필요할 수 있습니다.")
            }
            if (!auto.serviceOn) {
                OutlinedButton(onClick = {
                    context.startActivity(android.content.Intent(android.provider.Settings.ACTION_ACCESSIBILITY_SETTINGS)
                        .addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK))
                }) { Text("접근성 설정 열기") }
            }
            auto.last?.let { last ->
                val time = java.time.Instant.ofEpochMilli(last.at).atZone(java.time.ZoneId.systemDefault())
                    .format(java.time.format.DateTimeFormatter.ofPattern("M/d HH:mm"))
                val step = runCatching { AutoStep.valueOf(last.lastStep.orEmpty()).label }.getOrDefault("알 수 없음")
                val error = runCatching { AutoError.valueOf(last.error.orEmpty()).label }.getOrDefault("알 수 없음")
                Text(when (last.outcome) {
                    AutoShareOutcome.SUCCESS -> "마지막 자동 공유: 성공 ($time)"
                    AutoShareOutcome.FAILED -> "마지막 자동 공유: 실패 - $step · $error ($time, ${last.attempts}회째, 다시 시도합니다)"
                    AutoShareOutcome.GAVE_UP -> "마지막 자동 공유: 실패 - $step · $error ($time). ${last.attempts}회 실패해 자동 공유를 멈추고 수동 공유 알림으로 넘겼습니다."
                })
            }
            Text("미공유 통화는 매일 21:00 이후 요약 알림으로 알려드립니다. 백그라운드 실행 시각은 기기가 조정할 수 있습니다.")
        }
    }
}
