package io.github.junhcha.weaver.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Column
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.platform.LocalContext
import io.github.junhcha.weaver.calendar.ActionPhase
import io.github.junhcha.weaver.calendar.ActionState
import io.github.junhcha.weaver.calendar.CalendarRuntime
import io.github.junhcha.weaver.calendar.CalendarTarget
import io.github.junhcha.weaver.courier.CourierRuntime
import io.github.junhcha.weaver.schema.ActionKind
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Calendar changes approved on the PC run on their own. The user only chooses the target calendar once and
 * watches the state here; until a calendar is chosen the actions wait.
 */
@Composable
fun CalendarPanel() {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var actions by remember { mutableStateOf<List<ActionState>>(emptyList()) }
    var targets by remember { mutableStateOf<List<CalendarTarget>>(emptyList()) }
    var selected by remember { mutableStateOf<CalendarTarget?>(null) }
    var message by remember { mutableStateOf("") }
    LaunchedEffect(Unit) {
        do {
            runCatching { withContext(Dispatchers.IO) { CalendarRuntime.actions(context) to CalendarRuntime.selected(context) } }
                .onSuccess { (items, saved) -> actions = items; if (selected == null) selected = saved }
                .onFailure { message = "일정 상태를 읽을 수 없습니다" }
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
    if (actions.isNotEmpty()) Column {
        Text("승인 일정 · 자동 반영")
        Text("승인된 일정의 등록, 수정, 철회는 선택한 캘린더에 자동으로 반영됩니다. " +
            "선택한 계정의 설정에 따라 외부 서비스로 동기화될 수 있습니다. 참석자 초대는 보내지 않습니다.")
        OutlinedButton(onClick = {
            if (Feature.CALENDAR.isGranted(context)) load()
            else permission.launch(Feature.CALENDAR.permissions().toTypedArray())
        }) { Text("캘린더 선택 / 권한 요청") }
        targets.forEach { target ->
            OutlinedButton(onClick = {
                scope.launch {
                    runCatching { withContext(Dispatchers.IO) { CalendarRuntime.select(context, target) } }
                        .onSuccess { selected = target; CourierRuntime.kick(context) }.onFailure { message = "캘린더 선택 실패" }
                }
            }) { Text("${target.name} · ${target.account} · ${target.type}") }
        }
        selected?.let { Text("선택됨: ${it.name} · ${it.account}") }
        actions.takeLast(MAX_LISTED).reversed().forEach { CalendarActionRow(it, selected != null) }
        if (message.isNotEmpty()) Text(message)
    }
}

private const val MAX_LISTED = 8

@Composable
internal fun CalendarActionRow(state: ActionState, targetChosen: Boolean) {
    Text("${actionLabel(state.kind)} · ${state.title}")
    Text(actionStatus(state, targetChosen))
}

internal fun actionLabel(kind: ActionKind) = when (kind) {
    ActionKind.REGISTER -> "등록"
    ActionKind.UPDATE -> "수정"
    ActionKind.REMOVE -> "철회"
}

internal fun actionStatus(state: ActionState, targetChosen: Boolean): String = when (state.phase) {
    ActionPhase.DONE -> "완료"
    ActionPhase.FAILED -> when (state.error) {
        "manual_review" -> "포기 · 수동 확인 필요: 캘린더의 기존 일정을 확인하세요"
        "event_missing" -> "포기 · 캘린더에서 일정을 찾을 수 없습니다"
        else -> "포기 · ${state.attempts}회 시도했지만 실패했습니다"
    }
    ActionPhase.RETRY -> "재시도 대기 · ${state.attempts}회 실패"
    ActionPhase.PENDING -> when {
        !targetChosen || state.note == "target" -> "대기 중 · 대상 캘린더를 선택하세요"
        state.note == "permission" -> "대기 중 · 캘린더 권한이 필요합니다"
        else -> "대기 중"
    }
}
