package io.github.junhcha.weaver.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.phone.*
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch
import kotlinx.serialization.json.*

@Composable
internal fun NowHandlingBar(controller: NowController, onChanged: () -> Unit, onThread: (PhoneThread) -> Unit,
                            threads: List<PhoneThread>) {
    var list by remember { mutableStateOf(false) }
    var journal by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    var busy by remember { mutableStateOf(false) }
    var journalError by remember { mutableStateOf<String?>(null) }
    Column(Modifier.padding(horizontal = 16.dp)) {
        controller.snapshot?.let { if (!controller.online) Muted("오프라인 · ${it.generatedAt.atZone(it.zone).format(DateTimeFormatter.ofPattern("M/d HH:mm"))} 기준 사본") }
        Row {
            TextButton(enabled = controller.online && !busy, onClick = { if (controller.acknowledge()) onChanged() }) { Text("여기까지 확인") }
            if (controller.canEdit) TextButton(onClick = { list = true }) { Text("숨긴·미룬 항목") }
        }
        Muted("확인한 스냅샷까지 읽음으로 표시해요. 기한 지남·오늘 항목은 남아요.")
        if (controller.canEdit) TextButton(onClick = { journal = true }) { Text("전송·처리 이력") }
        controller.problem?.let { Muted(nowProblem(it)) }
    }
    if (list) HandlingSheet("숨긴·미룬 항목", { list = false }) {
        Muted("이 기기에서만 적용돼요. 재설치하면 초기화돼요. 숨김은 업무 취소가 아니에요.")
        val hidden = controller.suppressed()
        if (hidden.isEmpty()) Muted("숨긴·미룬 항목이 없어요")
        hidden.forEach { thread ->
            ThreadCard(thread, thread.deadline?.toString().orEmpty(), onClick = { list = false; onThread(thread) })
            TextButton(onClick = { controller.restore(thread); onChanged() }) { Text("다시 표시") }
        }
    }
    if (journal) HandlingSheet("전송·처리 이력", { journal = false }) {
        Muted("응답을 받지 못한 전송은 같은 키로 다시 확인해요.")
        TextButton(enabled = !busy, onClick = {
            busy = true
            scope.launch { try { controller.refresh(); onChanged() } finally { busy = false } }
        }) { Text("다시 연결·확인") }
        journalError?.let { Text(it) }
        controller.operations().asReversed().forEach { operation ->
            val target = operation.result?.get("thread_id")?.jsonPrimitive?.contentOrNull
                ?: operation.body["thread_id"]?.jsonPrimitive?.contentOrNull
            Text(operation.body["content"]?.jsonPrimitive?.content ?: "요청 처리 되돌리기")
            Muted(when (operation.state) {
                "pending" -> "응답 미확인 · 재연결하면 같은 키로 재전송"
                "applied" -> "반영 완료"
                "quarantined" -> "Fabric 세대가 달라 전송을 격리했어요. 새 세대에는 보내지 않아요."
                else -> nowProblem(operation.code ?: "edit_failed")
            })
            val thread = (controller.snapshot?.threads.orEmpty() + threads).firstOrNull { it.id == target }
            val editId = operation.result?.get("edit_thread_id")?.jsonPrimitive?.contentOrNull
            val alreadyUndone = controller.operations().any { it.state == "applied" && it.body["edit_thread_id"]?.jsonPrimitive?.contentOrNull == editId }
            if (operation.state == "applied" && operation.epoch == controller.snapshot?.epoch && operation.path != "/requests/undo" && editId != null && thread != null && !alreadyUndone) {
                TextButton(enabled = controller.online && !busy, onClick = {
                    busy = true
                    scope.launch {
                        try { controller.submit(thread, "undo", editThreadId = editId); onChanged() }
                        catch (cancel: CancellationException) { throw cancel }
                        catch (_: Exception) { journalError = "전송 상태가 바뀌었어요. 다시 연결·확인해 주세요." }
                        finally { busy = false }
                    }
                }) { Text("되돌리기") }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun HandlingSheet(title: String, dismiss: () -> Unit, content: @Composable ColumnScope.() -> Unit) {
    ModalBottomSheet(onDismissRequest = dismiss) {
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(title, style = MaterialTheme.typography.titleLarge)
            content()
            TextButton(onClick = dismiss) { Text("닫기") }
        }
    }
}

@Composable
internal fun NowThreadSheet(thread: PhoneThread, history: List<PhoneThread>, controller: NowController?,
                            onChanged: () -> Unit, dismiss: () -> Unit) {
    var action by remember(thread.id) { mutableStateOf<String?>(null) }
    var sentence by remember(thread.id) { mutableStateOf("이 요청을 완료했습니다: ${thread.content}".take(2000)) }
    var date by remember(thread.id) { mutableStateOf(thread.deadline?.toString().orEmpty()) }
    var custom by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    HandlingSheet("Thread 상세", dismiss) {
        Column(Modifier.testTag("now-thread-detail"), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(thread.content)
            Muted("${thread.kind.label} · ${thread.counterpart.orEmpty()}")
            Muted("기한 ${thread.deadline ?: "없음"} · ${if (thread.retracted == true) "철회됨" else if (thread.open == false && thread.kind == ThreadKind.REQUEST) "해결됨" else ""}")
            Text("출처: ${thread.source}")
            Muted("기록 시각 ${thread.recordedAt.atZone(controller?.snapshot?.zone ?: SeoulZone)}")
            Text("관련 이력")
            history.forEach { Text(it.content); Muted("${it.kind.label} · ${it.source}") }
            if (history.isEmpty()) Muted("관련 이력이 없어요")
            if (controller?.supported == true && controller.canEdit) {
                val pending = controller.operations().any { it.state == "pending" && it.body["thread_id"]?.jsonPrimitive?.contentOrNull == thread.id }
                if (pending) Muted("응답 미확인 전송이 있어요. 전송·처리 이력에서 확인해 주세요.")
                if (thread.kind == ThreadKind.REQUEST && thread.open == true && thread.retracted != true) {
                    Row {
                        TextButton(enabled = controller.online && !busy && !pending, onClick = { action = "complete"; sentence = "이 요청을 완료했습니다: ${thread.content}".take(2000) }) { Text("완료") }
                        TextButton(enabled = controller.online && !busy && !pending, onClick = { action = "deadline"; sentence = "이 요청의 기한을 ${date}로 변경합니다: ${thread.content}".take(2000) }) { Text("기한 변경") }
                    }
                    TextButton(onClick = { controller.snooze(thread, controller.tomorrow()); onChanged(); dismiss() }) { Text("내일 오전 9시에 다시 보기") }
                    OutlinedTextField(custom, { custom = it }, label = { Text("다시 볼 시각 (YYYY-MM-DD HH:mm)") }, modifier = Modifier.fillMaxWidth())
                    val customTime = runCatching { LocalDateTime.parse(custom, DateTimeFormatter.ofPattern("uuuu-MM-dd HH:mm").withResolverStyle(java.time.format.ResolverStyle.STRICT)).atZone(controller.snapshot!!.zone).toInstant() }.getOrNull()
                    TextButton(enabled = customTime?.isAfter(java.time.Instant.now()) == true, onClick = {
                        controller.snooze(thread, customTime!!); onChanged(); dismiss()
                    }) { Text("지정한 시각까지 미루기") }
                    Muted("${controller.snapshot?.zone} 기준 · 실제 기한은 바뀌지 않아요")
                }
                TextButton(onClick = { controller.hide(thread); onChanged(); dismiss() }) { Text("지금에서 숨기기") }
                Muted("이 기기에서만 숨겨요. 업무를 취소하지 않아요.")
                if (action != null) {
                    if (action == "deadline") OutlinedTextField(date, { date = it; sentence = "이 요청의 기한을 ${it}로 변경합니다: ${thread.content}".take(2000) }, label = { Text("새 기한 (YYYY-MM-DD)") }, modifier = Modifier.fillMaxWidth())
                    OutlinedTextField(sentence, { sentence = it.take(2000) }, label = { Text("기록할 문장 확인") }, modifier = Modifier.fillMaxWidth())
                    val deadline = runCatching { LocalDate.parse(date) }.getOrNull()
                    Button(enabled = controller.online && !busy && !pending && sentence.isNotBlank() && (action != "deadline" || deadline != null), onClick = {
                        busy = true
                        scope.launch {
                            try { controller.submit(thread, action!!, sentence, deadline); action = null; onChanged(); error = controller.problem?.let(::nowProblem) }
                            catch (cancel: CancellationException) { throw cancel }
                            catch (_: Exception) { error = "전송을 확인하지 못했어요. 전송·처리 이력에서 확인해 주세요." }
                            finally { busy = false }
                        }
                    }) { Text(if (busy) "전송 중" else "확인하고 기록") }
                }
            }
            error?.let { Text(it) }
        }
    }
}

private fun nowProblem(code: String): String = when (code) {
    "revision_conflict", "request_already_resolved", "request_retracted", "newer_deadline_change" -> "다른 변경이 먼저 반영됐어요. 새로고침한 내용과 이력을 확인한 뒤 다시 처리해 주세요."
    "epoch_mismatch" -> "Fabric 세대가 바뀌었어요. 이전 전송은 격리하고 새 목록을 불러와요."
    else -> "연결 또는 전송을 확인해 주세요 ($code)"
}
