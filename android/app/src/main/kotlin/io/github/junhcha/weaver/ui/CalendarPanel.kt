package io.github.junhcha.weaver.ui

import android.content.Intent
import android.net.Uri
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Column
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.platform.LocalContext
import io.github.junhcha.weaver.calendar.*
import io.github.junhcha.weaver.review.ReviewRecord
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** Always reachable from durable review history even after the PC removes its proposal. */
@Composable
fun CalendarPanel(record: ReviewRecord) {
    val proposal = remember(record) { CalendarRegistration.candidate(record) } ?: return
    if (proposal.schedules.isEmpty()) return
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var targets by remember { mutableStateOf<List<CalendarTarget>>(emptyList()) }
    var selected by remember { mutableStateOf<CalendarTarget?>(null) }
    var message by remember { mutableStateOf("캘린더를 선택한 뒤 등록 버튼을 누르세요") }
    var busy by remember { mutableStateOf(false) }
    var results by remember { mutableStateOf<List<String>>(emptyList()) }
    LaunchedEffect(proposal.proposalId, proposal.rev) {
        runCatching { withContext(Dispatchers.IO) {
            if (Feature.CALENDAR.isGranted(context)) {
                // Read-only provider reconciliation; a missing row still requires a registration tap.
                runCatching { CalendarRuntime.recover(context, proposal.proposalId, proposal.rev) }
            }
            CalendarRuntime.status(context, proposal.proposalId, proposal.rev) to CalendarRuntime.selected(context)
        } }.onSuccess { (saved, target) -> results = saved; selected = target }
            .onFailure { message = "등록 기록 읽기 실패 · 수동 재시도" }
    }
    val load = {
        scope.launch {
            busy = true
            runCatching { withContext(Dispatchers.IO) { CalendarRuntime.targets(context) to CalendarRuntime.selected(context) } }
                .onSuccess { (list, saved) ->
                    targets = list; selected = saved?.takeIf { it in list }
                    message = if (list.isEmpty()) "쓰기 가능한 캘린더 없음 · 계정/권한 확인 후 다시 시도" else "등록 대상을 선택하세요"
                }.onFailure { message = "권한과 캘린더를 확인하고 다시 시도하세요" }
            busy = false
        }
        Unit
    }
    val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        if (Feature.CALENDAR.isGranted(context)) load()
        else message = "캘린더 권한 거부 · 권한 재요청 또는 앱 설정 후 수동 재시도 가능"
    }
    Column {
        Text("승인 일정 · 사용자 등록 대기 (Verdict 공개 확인됨, PC 반영 확인 아님)")
        proposal.schedules.forEach {
            Text("${it.title} · ${it.start} ~ ${it.end ?: "종료 시각 없음, 1시간으로 등록"}" +
                if (it.blockedByRequest != null) " · 요청 미해소 · 등록 차단" else " · 시간 지정 일정")
        }
        Text("선택한 계정의 설정에 따라 외부 서비스로 동기화될 수 있습니다. 참석자 초대는 보내지 않습니다.")
        Text(message)
        OutlinedButton(enabled = !busy, onClick = {
            if (Feature.CALENDAR.isGranted(context)) load()
            else permission.launch(Feature.CALENDAR.permissions().toTypedArray())
        }) { Text("캘린더 선택 / 권한 재요청") }
        OutlinedButton(onClick = {
            context.startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:${context.packageName}")))
        }) { Text("앱 권한 설정") }
        targets.forEach { target ->
            OutlinedButton(enabled = !busy, onClick = {
                scope.launch {
                    busy = true
                    runCatching { withContext(Dispatchers.IO) { CalendarRuntime.select(context, target) } }
                        .onSuccess { selected = target }.onFailure { message = "선택 실패 · 권한 확인 후 재시도" }
                    busy = false
                }
            }) { Text("${target.name} · 계정 ${target.account} · 종류 ${target.type}") }
        }
        selected?.let { Text("선택됨: ${it.name} · ${it.account} · ${it.type}") }
        Text("재시도 시 이미 등록된 일정은 건너뛰고, 중단된 시도는 당시 선택한 캘린더에서 복구합니다.")
        OutlinedButton(enabled = !busy && selected != null && proposal.schedules.any { it.blockedByRequest == null }, onClick = {
            val target = checkNotNull(selected)
            busy = true
            scope.launch {
                runCatching { withContext(Dispatchers.IO) { CalendarRuntime.register(context, proposal.proposalId, proposal.rev, target) } }
                    .onSuccess { results = it }.onFailure { message = "등록 실패 · 승인 유지됨 · 권한/캘린더 확인 후 수동 재시도" }
                busy = false
            }
        }) { Text("승인 일정 등록 / 등록만 재시도") }
        results.forEach { Text(it) }
    }
}
