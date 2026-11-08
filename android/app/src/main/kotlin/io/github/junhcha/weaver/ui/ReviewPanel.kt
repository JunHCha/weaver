package io.github.junhcha.weaver.ui

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
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
    // Save the reviewed snapshot (including rev), never bind restored inputs to a newer rev.
    var proposalJson by rememberSaveable(proposalId) { mutableStateOf<String?>(null) }
    val proposal = remember(proposalJson) { proposalJson?.let { WeaverCodec.json.decodeFromString<Proposal>(it) } }
    var input by rememberSaveable(proposalId, stateSaver = ReviewInput.saver) { mutableStateOf(ReviewInput()) }
    var advancedExpanded by rememberSaveable(proposalId) { mutableStateOf(false) }
    var proposalMissing by remember(proposalId) { mutableStateOf(false) }
    var readError by remember(proposalId) { mutableStateOf<String?>(null) }
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
                val reviewedRev = proposalJson?.let { WeaverCodec.json.decodeFromString<Proposal>(it).rev }
                record = records.firstOrNull { it.verdict.proposalId == proposalId && it.verdict.rev == reviewedRev }
                // ReviewStore.latest reports this only after a successful listing with no candidate.
                // Permission, decoding and other read failures must still be visible.
                proposalMissing = result.exceptionOrNull()?.let {
                    it is IllegalStateException && it.message == "제안이 없습니다"
                } == true
                readError = if (result.isFailure && !(proposalMissing && record != null))
                    "제안을 읽을 수 없습니다. 최신 제안을 다시 확인하세요." else null
                result.onSuccess { fresh ->
                    latestRev = fresh.rev
                    if (proposalJson == null) {
                        proposalJson = WeaverCodec.encode(fresh)
                        input = input.copy(value = WeaverCodec.json.encodeToString(fresh.summary))
                    }
                }.onFailure { latestRev = null }
            }
            delay(5_000)
        } while (isActive)
    }
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedButton(onClick = onClose) { Text("검토 닫기") }
        val p = proposal
        if (p != null) {
            val inferred = proposalMissing && record != null
            val stale = latestRev != p.rev && !inferred
            ReviewSection("안내")
            Text(FACT_REVIEW_GUIDANCE)
            Text("요청 해소나 일정 실행 허가가 아닙니다.")
            ReviewSection("요약과 근거")
            Text("제안 r${p.rev} · ${p.summary}")
            Text("근거: ${p.threadExcerpt}")
            ReviewSection("추출 항목")
            p.entities.forEach { HorizontalDivider(); Text("인물·주제: ${it.name} · ${confidenceLabel(it.confidence)}\n${it.threadExcerpt}") }
            p.schedules.forEach { HorizontalDivider(); Text("일정: ${it.title} · ${it.start} ~ ${it.end ?: "미정"} · ${confidenceLabel(it.confidence)}\n${it.threadExcerpt}" +
                if (it.blockedByRequest != null) "\n요청 미해소 · 실행 보류" else "") }
            p.relations.forEach { HorizontalDivider(); Text("관계: ${it.from} → ${it.to} (${it.type}) · ${confidenceLabel(it.confidence)}\n${it.threadExcerpt}") }
            p.entryDrafts.forEach { HorizontalDivider(); Text("기록: ${it.kind} · ${it.value} · ${confidenceLabel(it.confidence)}\n${it.threadExcerpt}" +
                if (it.blockedByRequest != null || it.request != null) "\n요청·실행 허가는 별도" else "") }
            if (stale) {
                Text("새 rev 또는 파일 변경 감지 · 이전 rev는 제출할 수 없습니다")
                OutlinedButton(onClick = { error = null; onClose() }) { Text("목록으로 돌아가 최신 제안 확인") }
            }
            if (record != null) {
                Text(if (inferred) INFERRED_REVIEW_LABEL else record!!.status.label)
                if (!inferred && record!!.verdict.verdict == Verdict.Kind.REVISE) {
                    Text(if (latestRev != null && latestRev!! > p.rev) "새 rev 도착 · 최신 제안을 확인하세요"
                        else "다시 짠 새 rev를 기다립니다")
                }
                if (!inferred && record!!.status in setOf(ReviewStatus.ERROR, ReviewStatus.PENDING)) {
                    OutlinedButton(onClick = { ReviewRuntime.retry(context) }) { Text("동일한 검토 결과 재시도") }
                }
            }
            key(p.proposalId, p.rev) {
                val (path, value, edits, instruction, answers, reason) = input
                val enabled = !busy && !stale && record == null
                ReviewSection("확인할 질문")
                if (ReviewDraft.questions(p).isEmpty()) Text("확인할 질문 없음")
                ReviewDraft.questions(p).forEach { q ->
                    HorizontalDivider()
                    Text("질문 대상: ${q.summary} · ${q.confidenceLabel}")
                    Text(q.ask.question)
                    if (q.canConfirm) {
                        val confirmed = q.path in input.confirmations
                        OutlinedButton(enabled = enabled, onClick = {
                            input = input.copy(confirmations = if (confirmed) input.confirmations - q.path else input.confirmations + q.path)
                        }) { Text(if (confirmed) "확인 취소" else "이 내용이 맞음") }
                    } else if (q.ask.choices.isEmpty()) {
                        Text("일정 질문은 답을 입력하고 다시 짜기를 선택하세요")
                    }
                    q.ask.choices.forEach { c ->
                        OutlinedButton(enabled = enabled, onClick = { input = input.copy(answers = answers + (q.path to c.entityId)) }) { Text(c.label) }
                    }
                    OutlinedTextField(value = answers[q.path].orEmpty(), onValueChange = { input = input.copy(answers = answers + (q.path to it)) },
                        enabled = enabled, label = { Text("답 (자유 답변은 다시 짜기로 전송)") }, modifier = Modifier.fillMaxWidth())
                }
                if (ReviewDraft.hasMixedAnswers(p, answers, input.confirmations)) {
                    Text("자유 답변이 있어 다시 짜기로 전송되며, 확인한 항목이 다시 질문될 수 있습니다")
                }
                ReviewSection("수정")
                OutlinedButton(onClick = { advancedExpanded = !advancedExpanded }) {
                    Text(if (advancedExpanded) "고급: 직접 수정 접기" else "고급: 직접 수정")
                }
                if (advancedExpanded) {
                    Text("직접 수정: 예 /schedules/0/start · 값은 JSON 문자열이면 따옴표 포함")
                    OutlinedTextField(value = path, onValueChange = { input = input.copy(path = it) }, enabled = enabled, label = { Text("JSON Pointer") }, modifier = Modifier.fillMaxWidth())
                    OutlinedTextField(value = value, onValueChange = { input = input.copy(value = it) }, enabled = enabled, label = { Text("최종 값 (JSON)") }, modifier = Modifier.fillMaxWidth())
                    OutlinedButton(enabled = enabled, onClick = {
                        runCatching {
                            val next = edits + Verdict.Edit(path, WeaverCodec.json.parseToJsonElement(value))
                            ReviewDraft.validateEdits(p, next); input = input.copy(edits = next); error = null
                        }.onFailure { error = it.message }
                    }) { Text("수정 추가") }
                    edits.forEach { Text("${it.path} = ${it.value}") }
                    OutlinedButton(enabled = enabled, onClick = { input = input.copy(edits = emptyList()) }) { Text("수정 초기화") }
                }
                OutlinedTextField(value = instruction, onValueChange = { input = input.copy(instruction = it) }, enabled = enabled,
                    label = { Text("다시 짤 자연어 지시") }, modifier = Modifier.fillMaxWidth())
                ReviewSection("결정 버튼")
                Text("거절 사유: $reason")
                Verdict.RejectReason.entries.forEach { r ->
                    OutlinedButton(enabled = enabled, onClick = { input = input.copy(reason = r) }) { Text(r.name.lowercase()) }
                }
                Verdict.Kind.entries.forEach { kind ->
                    Text(when (kind) {
                        Verdict.Kind.APPROVE -> "승인: 이대로 기록"
                        Verdict.Kind.EDIT -> "수정 제출: 고친 내용으로 기록"
                        Verdict.Kind.REVISE -> "다시 짜기: 지시를 반영해 새 수정본을 받음"
                        Verdict.Kind.REJECT -> "거절: 기록하지 않음"
                    })
                    OutlinedButton(enabled = enabled && (kind != Verdict.Kind.APPROVE || edits.isEmpty()), onClick = {
                        busy = true; error = null
                        scope.launch {
                            val result = withContext(Dispatchers.IO) {
                                runCatching {
                                    val verdict = ReviewDraft.verdict(p, kind,
                                        if (kind == Verdict.Kind.EDIT || kind == Verdict.Kind.REVISE) edits else emptyList(),
                                        reason, instruction, answers, Instant.now().toString(), input.confirmations)
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
        readError?.let { Text(it) }
        error?.let { Text(it) }
    }
}

internal const val FACT_REVIEW_GUIDANCE = "통화 내용에서 뽑은 사실이 맞는지 확인합니다. 사람, 날짜·시각, 내용, 관계가 틀렸으면 고치세요. 철자·띄어쓰기·말투는 고치지 않아도 됩니다. 승인하면 기록과 노트에 사실로 남습니다."
internal const val INFERRED_REVIEW_LABEL = "전송됨 · 제안 파일이 정리되어 PC 반영으로 추정됩니다"

private fun confidenceLabel(value: Double): String = "확신도 ${kotlin.math.round(value * 100).toInt()}%"

@Composable
private fun ReviewSection(title: String) {
    HorizontalDivider(Modifier.padding(top = 8.dp))
    Text(title, style = MaterialTheme.typography.titleMedium)
}
