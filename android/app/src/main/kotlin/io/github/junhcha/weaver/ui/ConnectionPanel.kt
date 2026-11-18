package io.github.junhcha.weaver.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import io.github.junhcha.weaver.phone.SeoulZone
import io.github.junhcha.weaver.transport.ApiException
import io.github.junhcha.weaver.transport.PairingException
import io.github.junhcha.weaver.transport.PinMismatchException
import io.github.junhcha.weaver.transport.QueueCounts
import io.github.junhcha.weaver.transport.TransportFeature
import io.github.junhcha.weaver.transport.TransportRoute
import io.github.junhcha.weaver.transport.TransportRuntime
import io.github.junhcha.weaver.transport.UnreachableException
import java.time.Instant
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** Everything the connection card shows, read from settings and the spool. Stable codes only, no content. */
data class ConnectionUi(
    val paired: Boolean = false,
    val server: String? = null,
    val needsRepair: Boolean = false,
    val lastSyncAt: Long = 0L,
    val lastError: String? = null,
    val queue: QueueCounts = QueueCounts(0, 0, 0, 0),
    val routes: Map<TransportFeature, TransportRoute> = TransportFeature.entries.associateWith { TransportRoute.FILES },
)

internal fun ConnectionUi.statusText(): String = when {
    !paired -> "연결 안 됨"
    needsRepair -> "재페어링 필요 · 서버가 이 폰의 토큰을 거절했어요"
    lastError == "unreachable" -> "연결 대기 · Mac이나 VPN에 닿지 않아요"
    lastError == "pin_mismatch" -> "인증서가 달라요 · 다시 페어링하세요"
    lastError != null -> "오류 · $lastError"
    else -> "연결됨"
}

private fun syncText(at: Long): String {
    if (at == 0L) return "아직 없음"
    val time = Instant.ofEpochMilli(at).atZone(SeoulZone)
    return "${time.monthValue}/${time.dayOfMonth} ${"%02d:%02d".format(time.hour, time.minute)}"
}

/** "수집·설정" card: pairing (QR or pasted code), connection status, queue and the per-feature transport. */
@Composable
fun ConnectionContent(
    ui: ConnectionUi, busy: Boolean, message: String?,
    onScan: () -> Unit, onPair: (String) -> Unit, onRoute: (TransportFeature, TransportRoute) -> Unit,
    onRetry: () -> Unit, onUnpair: () -> Unit,
) {
    Card(Modifier.fillMaxWidth().testTag("connection-panel")) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("서버 연결 (Tailscale)", style = MaterialTheme.typography.titleMedium)
            Text(
                ui.statusText(), style = MaterialTheme.typography.bodyMedium,
                color = if (ui.paired && !ui.needsRepair && ui.lastError == null) WeaverTokens.ink else WeaverTokens.thread,
                modifier = Modifier.testTag("connection-status"),
            )
            if (!ui.paired || ui.needsRepair) {
                var code by remember { mutableStateOf("") }
                Muted("Mac에서 만든 페어링 QR을 스캔하세요. 코드를 직접 붙여 넣어도 돼요.")
                OutlinedButton(onClick = onScan, enabled = !busy, modifier = Modifier.defaultMinSize(minHeight = 48.dp).testTag("pair-scan")) {
                    Text("QR 스캔")
                }
                OutlinedTextField(code, { code = it }, Modifier.fillMaxWidth().testTag("pair-code"), label = { Text("페어링 코드") }, minLines = 2)
                OutlinedButton(
                    onClick = { onPair(code) }, enabled = !busy && code.isNotBlank(),
                    modifier = Modifier.defaultMinSize(minHeight = 48.dp).testTag("pair-submit"),
                ) { Text("연결") }
            } else {
                ui.server?.let { Muted("서버 $it") }
                Muted("마지막 동기화 ${syncText(ui.lastSyncAt)}", Modifier.testTag("connection-last-sync"))
                Muted(
                    "대기열 ${ui.queue.total}건 · 수집 ${ui.queue.captures} · 판정 ${ui.queue.verdicts} · 실행 결과 ${ui.queue.results}",
                    Modifier.testTag("connection-queue"),
                )
                if (ui.queue.blocked > 0) {
                    Text("조치가 필요한 항목 ${ui.queue.blocked}건", style = MaterialTheme.typography.bodySmall, color = WeaverTokens.thread)
                    TextButton(onClick = onRetry, Modifier.defaultMinSize(minHeight = 48.dp)) { Text("다시 시도") }
                }
                Text("전송 경로", style = MaterialTheme.typography.labelLarge)
                TransportFeature.entries.forEach { feature ->
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        Text(feature.label, Modifier.weight(1f), style = MaterialTheme.typography.bodyMedium)
                        TransportRoute.entries.forEach { route ->
                            PhoneChip(
                                route.label, ui.routes[feature] == route, { if (!busy) onRoute(feature, route) },
                                Modifier.testTag("route-${feature.wire}-${route.name}"),
                            )
                        }
                    }
                }
                Muted("기본은 Syncthing이에요. HTTP로 바꾸면 Mac이 꺼져 있는 동안 폰에 쌓아 두었다가 켜지면 보내요.")
                TextButton(onClick = onUnpair, Modifier.defaultMinSize(minHeight = 48.dp).testTag("unpair")) { Text("연결 해제") }
            }
            if (message != null) Text(message, style = MaterialTheme.typography.bodySmall, modifier = Modifier.testTag("connection-message"))
        }
    }
}

internal fun pairingErrorText(error: Throwable): String = when (error) {
    is PairingException -> error.message ?: "페어링 코드를 읽을 수 없어요"
    is PinMismatchException -> "서버 인증서가 코드의 지문과 달라요"
    is UnreachableException -> "서버에 연결하지 못했어요. Tailscale이 켜져 있는지 확인하세요"
    is ApiException ->
        if (error.status == 401 || error.status == 403) "서버가 이 코드를 거절했어요. 새 코드를 만드세요"
        else "서버가 응답하지 않았어요 (${error.code})"
    else -> "페어링하지 못했어요"
}

@Composable
fun ConnectionPanel() {
    val context = LocalContext.current.applicationContext
    val scope = rememberCoroutineScope()
    var tick by remember { mutableIntStateOf(0) }
    var ui by remember { mutableStateOf(ConnectionUi()) }
    var busy by remember { mutableStateOf(false) }
    var message by remember { mutableStateOf<String?>(null) }
    LifecycleResumeEffect(Unit) { tick++; onPauseOrDispose { } }
    LaunchedEffect(tick) {
        ui = withContext(Dispatchers.IO) {
            val settings = TransportRuntime.settings(context)
            ConnectionUi(
                settings.paired, settings.baseUrl, settings.needsRepair, settings.lastSyncAt, settings.lastError,
                TransportRuntime.queue(context), TransportFeature.entries.associateWith { settings.route(it) },
            )
        }
    }
    fun pair(text: String) {
        busy = true; message = null
        scope.launch {
            message = withContext(Dispatchers.IO) {
                try { TransportRuntime.pair(context, text); "연결했어요" } catch (error: Exception) { pairingErrorText(error) }
            }
            busy = false; tick++
        }
    }
    ConnectionContent(
        ui, busy, message,
        onScan = { scanQr(context, ::pair) { message = it } },
        onPair = ::pair,
        onRoute = { feature, route ->
            scope.launch {
                withContext(Dispatchers.IO) { TransportRuntime.setRoute(context, feature, route) }
                tick++
            }
        },
        onRetry = { scope.launch { withContext(Dispatchers.IO) { TransportRuntime.retryBlocked(context) }; tick++ } },
        onUnpair = { scope.launch { withContext(Dispatchers.IO) { TransportRuntime.unpair(context) }; message = null; tick++ } },
    )
}

/** Google's code scanner (Play services) reads the pairing QR; no camera permission is needed. */
private fun scanQr(context: android.content.Context, onText: (String) -> Unit, onProblem: (String) -> Unit) {
    val options = com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions.Builder()
        .setBarcodeFormats(com.google.mlkit.vision.barcode.common.Barcode.FORMAT_QR_CODE).build()
    com.google.mlkit.vision.codescanner.GmsBarcodeScanning.getClient(context, options).startScan()
        .addOnSuccessListener { code -> code.rawValue?.let(onText) ?: onProblem("QR을 읽지 못했어요") }
        .addOnFailureListener { onProblem("QR 스캐너를 열 수 없어요. 코드를 붙여 넣어 주세요") }
}
