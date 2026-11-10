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
import io.github.junhcha.weaver.schema.WeaverCodec
import io.github.junhcha.weaver.schema.v2.ReviewPackage
import io.github.junhcha.weaver.schema.v2.Verdict
import kotlinx.coroutines.*

@Composable
fun ReviewV2Panel(id: String, onClose: () -> Unit) {
    val context = LocalContext.current.applicationContext
    val scope = rememberCoroutineScope()
    val changes by InboxRuntime.changes.collectAsState()
    var snapshot by rememberSaveable(id) { mutableStateOf<String?>(null) }
    val p = remember(snapshot) { snapshot?.let { WeaverCodec.json.decodeFromString<ReviewPackage>(it) } }
    var answers by rememberSaveable(id) { mutableStateOf(arrayListOf<String>()) }
    var body by rememberSaveable(id) { mutableStateOf("") }
    var feedback by rememberSaveable(id) { mutableStateOf("") }
    var finalReview by rememberSaveable(id) { mutableStateOf(false) }
    var details by rememberSaveable(id) { mutableStateOf(false) }
    var actionInput by rememberSaveable(id) { mutableStateOf<String?>(null) }
    var latest by remember(id) { mutableStateOf<ReviewPackage?>(null) }
    var record by remember(id) { mutableStateOf<ReviewRecordV2?>(null) }
    var readError by remember(id) { mutableStateOf<String?>(null) }
    var error by remember(id) { mutableStateOf<String?>(null) }
    var busy by remember(id) { mutableStateOf(false) }
    LaunchedEffect(id, changes) {
        do {
            val (result, records) = withContext(Dispatchers.IO) {
                runCatching { ReviewRuntime.openPackage(context, id) } to ReviewRuntime.recordsV2(context)
            }
            val reviewed = snapshot?.let { WeaverCodec.json.decodeFromString<ReviewPackage>(it) } ?: result.getOrNull()
            record = records.firstOrNull { it.verdict.packageId == id && it.verdict.revision == reviewed?.revision }
            result.onSuccess { fresh ->
                latest = fresh; readError = null
                if (snapshot == null) {
                    snapshot = WeaverCodec.encode(fresh)
                    answers = ArrayList(List(fresh.questions.size) { "" })
                    body = fresh.draftBody
                }
            }.onFailure { latest = null; readError = "문서를 읽을 수 없습니다. 최신 문서를 확인하세요." }
            delay(5_000)
        } while (isActive)
    }
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedButton(onClick = onClose) { Text("검토 닫기") }
        if (p != null) {
            val stale = latest != p
            val enabled = !busy && !stale && record == null
            Text("요약", style = MaterialTheme.typography.titleMedium)
            val firstLine = p.draftBody.lineSequence().first()
            Text(firstLine.take(80) + if (firstLine.length > 80) "…" else "")
            OutlinedButton(onClick = { details = !details }) { Text(if (details) "본문 접기" else "본문 보기") }
            if (details) Text(p.draftBody)
            record?.let {
                Text(it.status.label)
                if (it.status in setOf(ReviewStatus.ERROR, ReviewStatus.PENDING))
                    OutlinedButton(onClick = { ReviewRuntime.retry(context) }) { Text("재시도") }
            }
            if (stale && record == null) Text("최신 문서를 다시 확인하세요")
            if (!finalReview) {
                Text("확인할 질문", style = MaterialTheme.typography.titleMedium)
                if (p.questions.isEmpty()) Text("확인할 질문 없음")
                p.questions.forEachIndexed { index, q ->
                    Text(q.question)
                    q.choices.forEach { choice ->
                        val selected = answers.getOrNull(index) == choice
                        OutlinedButton(enabled = enabled, onClick = {
                            answers = ArrayList(answers).apply { set(index, choice) }
                        }) { Text(if (selected) "✓ $choice" else choice) }
                    }
                    var rationale by rememberSaveable(id, index) { mutableStateOf(false) }
                    OutlinedButton(onClick = { rationale = !rationale }) { Text(if (rationale) "이유 접기" else "질문 이유 보기") }
                    if (rationale) Text(q.rationale)
                }
                OutlinedButton(enabled = enabled && answers.size == p.questions.size && answers.all { it.isNotEmpty() },
                    onClick = { finalReview = true }) { Text("최종 검토") }
            } else {
                Text("최종 검토", style = MaterialTheme.typography.titleMedium)
                p.questions.forEachIndexed { index, q -> Text("${q.question}: ${answers.getOrElse(index) { "" }}") }
                OutlinedButton(enabled = enabled, onClick = { finalReview = false }) { Text("답변 수정") }
                if (actionInput == "EDIT") OutlinedTextField(value = body, onValueChange = { body = it }, enabled = enabled,
                    label = { Text("직접 수정할 본문") }, modifier = Modifier.fillMaxWidth())
                if (actionInput == "FEEDBACK") OutlinedTextField(value = feedback, onValueChange = { feedback = it }, enabled = enabled,
                    label = { Text("피드백 내용") }, modifier = Modifier.fillMaxWidth())
                Verdict.Action.entries.forEach { action ->
                    OutlinedButton(enabled = enabled, onClick = {
                        if (action in setOf(Verdict.Action.EDIT, Verdict.Action.FEEDBACK) && actionInput != action.name) {
                            actionInput = action.name
                            return@OutlinedButton
                        }
                        busy = true; error = null
                        scope.launch {
                            val result = withContext(Dispatchers.IO) { runCatching {
                                val verdict = ReviewV2Draft.verdict(p, answers, action, body, feedback)
                                ReviewRuntime.submit(context, p, verdict)
                                ReviewRuntime.recordsV2(context).first { it.verdict == verdict }
                            } }
                            result.onSuccess { record = it }.onFailure { error = it.message ?: "전송 준비 실패" }
                            busy = false
                        }
                    }) { Text(when (action) {
                        Verdict.Action.APPROVE -> "승인"; Verdict.Action.EDIT -> if (actionInput == "EDIT") "수정 제출" else "본문 직접 수정"
                        Verdict.Action.FEEDBACK -> if (actionInput == "FEEDBACK") "피드백 제출" else "자연어 피드백"; Verdict.Action.REJECT -> "거절"
                    }) }
                }
            }
        }
        if (record == null) readError?.let { Text(it) }
        error?.let { Text(it) }
    }
}
