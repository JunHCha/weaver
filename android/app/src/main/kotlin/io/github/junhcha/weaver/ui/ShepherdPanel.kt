package io.github.junhcha.weaver.ui

import android.app.NotificationManager
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.core.app.NotificationManagerCompat
import io.github.junhcha.weaver.shepherd.CallRecord
import io.github.junhcha.weaver.shepherd.CallStatus
import io.github.junhcha.weaver.shepherd.ShepherdRuntime
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext

@Composable
fun ShepherdPanel(showPendingCalls: Boolean = false) {
    val context = LocalContext.current
    var activated by remember { mutableStateOf(false) }
    var calls by remember { mutableStateOf(emptyList<CallRecord>()) }
    var onlyPending by remember(showPendingCalls) { mutableStateOf(showPendingCalls) }
    LaunchedEffect(Unit) {
        while (true) {
            withContext(Dispatchers.IO) {
                activated = ShepherdRuntime.isActivated(context)
                calls = ShepherdRuntime.calls(context)
            }
            delay(2_000)
        }
    }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(if (onlyPending) "스크립트 미공유 통화" else "Shepherd 통화 목록")
            if (!activated) {
                Text("활성화 이후 통화만 감지합니다. 과거 통화 가져오기는 별도 기능입니다.")
                OutlinedButton(onClick = {
                    ShepherdRuntime.activate(context)
                    activated = true
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
                Text(if (call.status == CallStatus.UNSHARED) "스크립트 미공유" else "처리됨")
            }
            Text("미공유 통화는 매일 21:00 이후 요약 알림으로 알려드립니다. 백그라운드 실행 시각은 기기가 조정할 수 있습니다.")
        }
    }
}
