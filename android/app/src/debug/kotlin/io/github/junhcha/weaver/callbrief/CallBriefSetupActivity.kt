package io.github.junhcha.weaver.callbrief

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.ui.WeaverTheme

class CallBriefSetupActivity : ComponentActivity() {
    private var revision by mutableIntStateOf(0)
    private var message by mutableStateOf("")
    private val request = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { revision++ }
    private val permissions get() = buildList {
        add(Manifest.permission.READ_PHONE_STATE)
        add(Manifest.permission.READ_CALL_LOG)
        add(Manifest.permission.READ_CONTACTS)
        if (Build.VERSION.SDK_INT >= 33) add(Manifest.permission.POST_NOTIFICATIONS)
    }

    override fun onResume() { super.onResume(); revision++ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            WeaverTheme {
                Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
                    Column(Modifier.padding(24.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                        Text("통화 브리핑 시험 · 머지 금지", style = MaterialTheme.typography.headlineSmall)
                        Text("가상 브리핑만 표시합니다. 연락처 이름은 메모리에서 해석 성공 여부만 확인하며 표시·저장·전송하지 않습니다. 시험용 연락처와 번호를 쓰세요.")
                        Text("1. 다른 앱 위에 표시 → 2. 전화 상태·통화 기록·연락처·알림 → 3. 감시 시작")
                        val tick = revision
                        Text("겹침: ${Settings.canDrawOverlays(this@CallBriefSetupActivity)} · 상태 갱신 $tick")
                        Button(onClick = { startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName"))) }) { Text("다른 앱 위에 표시 설정") }
                        permissions.forEach { permission ->
                            Text("${permission.substringAfterLast('.')} : ${if (checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED) "허용" else "거절/미요청"}")
                        }
                        Button(onClick = { request.launch(permissions.toTypedArray()) }) { Text("권한 요청 (거절해도 앱 사용 가능)") }
                        Text("영구 거절 또는 통화 기록 권한이 제한되면 앱 설정에서 확인하세요. 번호 없는 통화는 이름 미확인으로 계속합니다.")
                        TextButton(onClick = { startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName"))) }) { Text("앱 권한 설정") }
                        Button(onClick = {
                            if (checkSelfPermission(Manifest.permission.READ_PHONE_STATE) != PackageManager.PERMISSION_GRANTED || !Settings.canDrawOverlays(this@CallBriefSetupActivity)) {
                                message = "전화 상태와 겹침 권한이 필요합니다."
                            } else {
                                runCatching { startForegroundService(Intent(this@CallBriefSetupActivity, CallBriefService::class.java)) }
                                    .onSuccess { message = "감시 시작. 알림에서 중지할 수 있습니다." }
                                    .onFailure { message = "서비스 시작 실패: ${it.javaClass.simpleName}" }
                            }
                        }) { Text("통화 감시 시작") }
                        TextButton(onClick = { stopService(Intent(this@CallBriefSetupActivity, CallBriefService::class.java)); message = "감시 중지" }) { Text("감시 중지 · 겹침 제거") }
                        var multiple by remember { mutableStateOf(getPreferences(0).getBoolean("multiple", true)) }
                        Row {
                            Checkbox(checked = multiple, onCheckedChange = {
                                multiple = it
                                getSharedPreferences("callbrief", 0).edit().putBoolean("multiple", it).apply()
                                getPreferences(0).edit().putBoolean("multiple", it).apply()
                            })
                            Text("NEW_TASK + MULTIPLE_TASK (해제: NEW_TASK만)")
                        }
                        Button(onClick = { CallBriefWindows.launch(this@CallBriefSetupActivity) }) { Text("팝업 경계 시험 · 자동 카드 대안") }
                        Text("기기 freeform feature: ${packageManager.hasSystemFeature(PackageManager.FEATURE_FREEFORM_WINDOW_MANAGEMENT)}")
                        Text("표준 경계 API가 전체 화면을 열면 즉시 닫고 카드로 바꿉니다. 실제 팝업 여부는 기기에서 확인하세요.")
                        Text(message)
                    }
                }
            }
        }
    }
}
