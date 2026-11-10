package io.github.junhcha.weaver.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Column
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.platform.LocalContext
import io.github.junhcha.weaver.calendar.CalendarRuntime
import io.github.junhcha.weaver.calendar.CalendarTarget
import io.github.junhcha.weaver.schema.v2.ApprovedSchedule
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

@Composable
fun CalendarV2Panel(packageId: String) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var schedules by remember(packageId) { mutableStateOf<List<ApprovedSchedule>>(emptyList()) }
    var targets by remember { mutableStateOf<List<CalendarTarget>>(emptyList()) }
    var selected by remember { mutableStateOf<CalendarTarget?>(null) }
    var message by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    LaunchedEffect(packageId) {
        do {
            runCatching { withContext(Dispatchers.IO) { CalendarRuntime.schedules(context, packageId) } }
                .onSuccess { schedules = it }.onFailure { schedules = emptyList(); message = "일정을 읽을 수 없습니다" }
            delay(5_000)
        } while (isActive)
    }
    val load = {
        scope.launch {
            runCatching { withContext(Dispatchers.IO) { CalendarRuntime.targets(context) to CalendarRuntime.selected(context) } }
                .onSuccess { (items, saved) -> targets = items; selected = items.firstOrNull { saved?.sameIdentity(it) == true } }
                .onFailure { message = "권한과 캘린더를 확인하세요" }
        }
        Unit
    }
    val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        if (Feature.CALENDAR.isGranted(context)) load() else message = "캘린더 권한이 필요합니다"
    }
    if (schedules.isNotEmpty()) Column {
        Text("승인 일정 · 사용자 등록")
        Text("선택한 계정의 설정에 따라 외부 서비스로 동기화될 수 있습니다. 참석자 초대는 보내지 않습니다.")
        OutlinedButton(onClick = {
            if (Feature.CALENDAR.isGranted(context)) load()
            else permission.launch(Feature.CALENDAR.permissions().toTypedArray())
        }) { Text("캘린더 선택 / 권한 요청") }
        targets.forEach { target ->
            OutlinedButton(onClick = {
                scope.launch {
                    runCatching { withContext(Dispatchers.IO) { CalendarRuntime.select(context, target) } }
                        .onSuccess { selected = target }.onFailure { message = "캘린더 선택 실패" }
                }
            }) { Text("${target.name} · ${target.account} · ${target.type}") }
        }
        selected?.let { Text("선택됨: ${it.name} · ${it.account}") }
        schedules.forEach { schedule ->
            CalendarV2Schedule(schedule, !busy && selected != null) {
                    val target = checkNotNull(selected)
                    busy = true
                    scope.launch {
                        runCatching { withContext(Dispatchers.IO) { CalendarRuntime.registerV2(context, packageId, schedule.threadId, target) } }
                            .onSuccess { message = it }.onFailure { message = "등록 실패 · 다시 시도하세요" }
                        busy = false
                    }
            }
        }
        if (message.isNotEmpty()) Text(message)
    }
}

@Composable
internal fun CalendarV2Schedule(schedule: ApprovedSchedule, enabled: Boolean, onRegister: () -> Unit) {
    Text("${schedule.title} · ${schedule.start} ~ ${schedule.end ?: "1시간"}")
    when {
        schedule.retracted -> Text("철회됨 · 캘린더에서 직접 등록 해제하세요")
        schedule.blockedBy != null -> Text("요청 ${schedule.blockedBy} 미해소 · 등록 차단")
        else -> OutlinedButton(enabled = enabled, onClick = onRegister) { Text("캘린더에 등록") }
    }
}
