package io.github.junhcha.weaver.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Column
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.platform.LocalContext
import io.github.junhcha.weaver.BuildConfig
import io.github.junhcha.weaver.courier.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

@Composable
fun CourierPanel() {
    val context = LocalContext.current.applicationContext
    val scope = rememberCoroutineScope()
    var status by remember { mutableStateOf(CourierRuntime.status(context)) }
    var busy by remember { mutableStateOf(false) }
    var diagnostics by remember { mutableStateOf<List<String>>(emptyList()) }
    var showDiagnostics by remember { mutableStateOf(false) }
    DisposableEffect(context) {
        val preferences = context.getSharedPreferences("courier", android.content.Context.MODE_PRIVATE)
        val listener = android.content.SharedPreferences.OnSharedPreferenceChangeListener { _, key ->
            if (key == "status") status = CourierRuntime.status(context)
        }
        preferences.registerOnSharedPreferenceChangeListener(listener)
        onDispose { preferences.unregisterOnSharedPreferenceChangeListener(listener) }
    }
    fun run(action: () -> String) {
        busy = true
        scope.launch {
            status = withContext(Dispatchers.IO) {
                try { synchronized(CourierRuntime.lock) { action() } }
                catch (error: Exception) { error.message ?: "Courier 작업 실패" }
            }
            busy = false
        }
    }
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        val data = result.data
        val uri = data?.data
        if (result.resultCode == android.app.Activity.RESULT_OK && uri != null) run {
            CourierRuntime.select(context, uri, data.flags).joinToString("\n")
        }
    }
    Column {
        Text("문서 동기화 설정")
        if (showDiagnostics) Text(status)
        OutlinedButton(enabled = !busy, onClick = { picker.launch(CourierRuntime.picker(context)) }) {
            Text("동기화 폴더 선택 / 권한 복구")
        }
        if (BuildConfig.DEBUG) {
            OutlinedButton(enabled = !busy, onClick = {
                showDiagnostics = true
                run {
                    val permission = runCatching { CourierRuntime.folder(context); "영구 읽기·쓰기 권한 정상" }
                        .getOrElse { "권한 확인 실패: 폴더를 다시 선택하세요" }
                    CourierDatabase(context).use { db ->
                        runCatching { CourierClient(CourierRuntime.folder(context), db).scan() }
                        diagnostics = db.diagnostics()
                    }
                    permission
                }
            }) { Text("Courier 진단") }
            if (showDiagnostics) {
                Text("자동 정리 OFF · 충돌 사본 및 거부 파일 (발견 시각)")
                diagnostics.forEach { Text(it) }
                OutlinedButton(enabled = !busy, onClick = { run {
                    CourierRuntime.folder(context).selfTest().joinToString("\n")
                } }) { Text("자체 시험: 네 폴더 쓰기·rename·읽기·삭제") }
                OutlinedButton(enabled = !busy, onClick = { run {
                    CourierRuntime.testAtomicity(context)
                } }) { Text("원자성 시험 (합성 16 MiB)") }
                OutlinedButton(enabled = !busy, onClick = { run {
                    val source = java.io.File(context.cacheDir, "courier-synthetic-${java.util.UUID.randomUUID()}.txt")
                    try {
                        source.outputStream().use { output ->
                            val chunk = ByteArray(64 * 1024) { 0x5a }
                            repeat(1600) { output.write(chunk) }
                        }
                        val manifest = CourierRuntime.prepare(context, android.net.Uri.fromFile(source),
                            io.github.junhcha.weaver.schema.CaptureKind.TEXT,
                            io.github.junhcha.weaver.schema.Channel.SHARE_TEXT, "txt")
                        "합성 100 MiB 준비 완료: ${manifest.captureId} · ${manifest.sha256}"
                    } finally { source.delete() }
                } }) { Text("합성 캡처 100 MiB 전송 (시험 폴더에서만)") }
                Text("로컬 시험 성공 후에도 Fold/Syncthing 왕복·프로세스 종료 검증이 필요합니다.")
            }
        }
    }
}
