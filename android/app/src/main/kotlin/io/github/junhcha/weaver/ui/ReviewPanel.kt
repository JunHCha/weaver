package io.github.junhcha.weaver.ui

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.review.*
import io.github.junhcha.weaver.schema.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.withContext
import java.time.Instant

/** Deliberately minimal proposal-level form. Every edit value is explicit JSON, including strings. */
@Composable
fun ReviewPanel(proposalId: String, onClose: () -> Unit) {
    val context = LocalContext.current.applicationContext
    val scope = rememberCoroutineScope()
    val changes by InboxRuntime.changes.collectAsState()
    var proposal by remember(proposalId) { mutableStateOf<Proposal?>(null) }
    var latestRev by remember(proposalId) { mutableStateOf<Long?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    var record by remember { mutableStateOf<ReviewRecord?>(null) }
    // SAF has no portable watch API. Re-read the revision only while this form is open;
    // submission and publication also check it immediately before writing.
    LaunchedEffect(proposalId, changes) {
        do {
            withContext(Dispatchers.IO) {
                val result = runCatching { ReviewRuntime.open(context, proposalId) }
                val records = ReviewRuntime.records(context)
                result to records
            }.let { (result, records) ->
                result.onSuccess { fresh ->
                    latestRev = fresh.rev
                    if (proposal == null) proposal = fresh
                }.onFailure { latestRev = null; error = "제안을 읽을 수 없습니다. 최신 제안을 다시 확인하세요." }
                record = records.firstOrNull { it.verdict.proposalId == proposalId && it.verdict.rev == proposal?.rev }
            }
            delay(5_000)
        } while (isActive)
    }
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedButton(onClick = onClose) { Text("검토 닫기") }
        val p = proposal
        if (p != null) {
            val stale = latestRev != p.rev
            Text("제안 r${p.rev} · ${p.summary}")
            Text("근거: ${p.threadExcerpt}")
            p.entities.forEach { Text("인물·주제: ${it.name} · confidence ${it.confidence}\n${it.threadExcerpt}") }
            p.schedules.forEach { Text("일정: ${it.title} · ${it.start} ~ ${it.end ?: "미정"} · confidence ${it.confidence}\n${it.threadExcerpt}" +
                if (it.blockedByRequest != null) "\n요청 미해소 · 실행 보류" else "") }
            p.relations.forEach { Text("관계: ${it.from} → ${it.to} (${it.type}) · confidence ${it.confidence}\n${it.threadExcerpt}") }
            p.entryDrafts.forEach { Text("기록: ${it.kind} · ${it.value} · confidence ${it.confidence}\n${it.threadExcerpt}" +
                if (it.blockedByRequest != null || it.request != null) "\n요청·실행 허가는 별도" else "") }
            Text("승인·수정은 사실 검토입니다. 요청 해소나 일정 실행 허가가 아닙니다.")
            if (stale) {
                Text("새 rev 또는 파일 변경 감지 · 이전 rev는 제출할 수 없습니다")
                OutlinedButton(onClick = { proposal = null; error = null; onClose() }) { Text("목록으로 돌아가 최신 제안 확인") }
            }
            if (record != null) {
                Text(record!!.status.label)
                if (record!!.verdict.verdict == Verdict.Kind.REVISE) {
                    Text(if (latestRev != null && latestRev!! > p.rev) "새 rev 도착 · 최신 제안을 확인하세요"
                        else "다시 짠 새 rev를 기다립니다")
                }
                if (record!!.status in setOf(ReviewStatus.ERROR, ReviewStatus.PENDING)) {
                    OutlinedButton(onClick = { ReviewRuntime.retry(context) }) { Text("동일한 검토 결과 재시도") }
                }
            }
            key(p.proposalId, p.rev) {
                var path by remember { mutableStateOf("/summary") }
                var value by remember { mutableStateOf(WeaverCodec.json.encodeToString(p.summary)) }
                var edits by remember { mutableStateOf(emptyList<Verdict.Edit>()) }
                var instruction by remember { mutableStateOf("") }
                var answers by remember { mutableStateOf(emptyMap<String, String>()) }
                var reason by remember { mutableStateOf(Verdict.RejectReason.OTHER) }
                val enabled = !busy && !stale && record == null
                ReviewDraft.questions(p).forEach { q ->
                    Text("질문 ${q.path}: ${q.ask.question}")
                    q.ask.choices.forEach { c ->
                        OutlinedButton(enabled = enabled, onClick = { answers = answers + (q.path to c.entityId) }) { Text(c.label) }
                    }
                    OutlinedTextField(value = answers[q.path].orEmpty(), onValueChange = { answers = answers + (q.path to it) },
                        enabled = enabled, label = { Text("답 (자유 답변은 다시 짜기로 전송)") }, modifier = Modifier.fillMaxWidth())
                }
                Text("직접 수정: 예 /schedules/0/start · 값은 JSON 문자열이면 따옴표 포함")
                OutlinedTextField(value = path, onValueChange = { path = it }, enabled = enabled, label = { Text("JSON Pointer") })
                OutlinedTextField(value = value, onValueChange = { value = it }, enabled = enabled, label = { Text("최종 값 (JSON)") })
                OutlinedButton(enabled = enabled, onClick = {
                    runCatching {
                        val next = edits + Verdict.Edit(path, WeaverCodec.json.parseToJsonElement(value))
                        ReviewDraft.validateEdits(p, next); edits = next; error = null
                    }.onFailure { error = it.message }
                }) { Text("수정 추가") }
                edits.forEach { Text("${it.path} = ${it.value}") }
                OutlinedButton(enabled = enabled, onClick = { edits = emptyList() }) { Text("수정 초기화") }
                OutlinedTextField(value = instruction, onValueChange = { instruction = it }, enabled = enabled,
                    label = { Text("다시 짤 자연어 지시") }, modifier = Modifier.fillMaxWidth())
                Text("거절 사유: $reason")
                Verdict.RejectReason.entries.forEach { r ->
                    OutlinedButton(enabled = enabled, onClick = { reason = r }) { Text(r.name.lowercase()) }
                }
                Verdict.Kind.entries.forEach { kind ->
                    OutlinedButton(enabled = enabled && (kind != Verdict.Kind.APPROVE || edits.isEmpty()), onClick = {
                        busy = true; error = null
                        scope.launch {
                            val result = withContext(Dispatchers.IO) {
                                runCatching {
                                    val verdict = ReviewDraft.verdict(p, kind,
                                        if (kind == Verdict.Kind.EDIT || kind == Verdict.Kind.REVISE) edits else emptyList(),
                                        reason, instruction, answers, Instant.now().toString())
                                    ReviewRuntime.submit(context, p, verdict)
                                    ReviewRuntime.records(context).first { it.verdict == verdict }
                                }
                            }
                            result.onSuccess { record = it }.onFailure { error = it.message ?: "전송 준비 실패" }
                            busy = false
                        }
                    }) { Text(when (kind) {
                        Verdict.Kind.APPROVE -> "승인"; Verdict.Kind.EDIT -> "수정 제출"
                        Verdict.Kind.REVISE -> "다시 짜기"; Verdict.Kind.REJECT -> "거절"
                    }) }
                }
                if (busy) Text("전송 준비 중")
            }
        }
        error?.let { Text(it) }
    }
}
