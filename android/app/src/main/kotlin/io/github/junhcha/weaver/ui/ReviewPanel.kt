package io.github.junhcha.weaver.ui

import androidx.compose.foundation.text.ClickableText
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.review.*
import io.github.junhcha.weaver.schema.WeaverCodec
import io.github.junhcha.weaver.schema.ReviewPackage
import io.github.junhcha.weaver.schema.Verdict
import kotlinx.coroutines.*

@Composable
fun ReviewPanel(
    id: String,
    onSubmitted: () -> Unit = {},
    onBusyChanged: (Boolean) -> Unit = {},
    showCloseButton: Boolean = true,
    wide: Boolean = false,
    onClose: () -> Unit,
) {
    val context = LocalContext.current.applicationContext
    val scope = rememberCoroutineScope()
    val changes by InboxRuntime.changes.collectAsState()
    var snapshot by rememberSaveable(id, key = "review-snapshot") { mutableStateOf<String?>(null) }
    val p = remember(snapshot) { snapshot?.let { WeaverCodec.json.decodeFromString<ReviewPackage>(it) } }
    var answers by rememberSaveable(id, key = "review-answers") { mutableStateOf(arrayListOf<String>()) }
    var body by rememberSaveable(id, key = "review-body") { mutableStateOf("") }
    var feedback by rememberSaveable(id, key = "review-feedback") { mutableStateOf("") }
    var finalReview by rememberSaveable(id, key = "review-finalReview") { mutableStateOf(false) }
    var latest by remember(id) { mutableStateOf<ReviewPackage?>(null) }
    var record by remember(id) { mutableStateOf<ReviewRecord?>(null) }
    var readError by remember(id) { mutableStateOf<String?>(null) }
    var error by remember(id) { mutableStateOf<String?>(null) }
    val preferences = remember(context) { context.getSharedPreferences("review-settings", android.content.Context.MODE_PRIVATE) }
    var vault by remember { mutableStateOf(preferences.getString("vault", "").orEmpty()) }
    var showLinks by rememberSaveable(id, key = "review-showLinks") { mutableStateOf(false) }
    var showSource by rememberSaveable(id, key = "review-showSource") { mutableStateOf(false) }
    var busy by remember(id) { mutableStateOf(false) }
    var pendingReject by remember(id) { mutableStateOf<PendingReject?>(null) }
    var undoExpired by remember(id) { mutableStateOf(false) }
    LaunchedEffect(id, changes) {
        do {
            val (result, records) = withContext(Dispatchers.IO) {
                runCatching { ReviewRuntime.open(context, id) } to ReviewRuntime.records(context)
            }
            val reviewed = snapshot?.let { WeaverCodec.json.decodeFromString<ReviewPackage>(it) } ?: result.getOrNull()
            pendingReject = withContext(Dispatchers.IO) { ReviewRuntime.pendingRejects(context) }
                .firstOrNull { it.packageId == id && it.revision == reviewed?.revision }
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
    LaunchedEffect(pendingReject) {
        val entry = pendingReject
        undoExpired = false
        if (entry != null) {
            delay((entry.fireAt - System.currentTimeMillis()).coerceAtLeast(0))
            undoExpired = true
        }
    }
    val content: @Composable ColumnScope.() -> Unit = {
        if (showCloseButton) OutlinedButton(onClick = onClose) { Text("검토 닫기") }
        if (p != null) {
            val stale = latest != p
            val enabled = !busy && !stale && record == null
            Text(p.reviewTitle(), style = MaterialTheme.typography.titleLarge)
            if (pendingReject != null && record == null) {
                OutlinedButton(enabled = !undoExpired, modifier = Modifier.testTag("review-undo"), onClick = {
                    scope.launch {
                        val undone = withContext(Dispatchers.IO) { ReviewRuntime.cancelReject(context, id) }
                        if (undone) pendingReject = null else undoExpired = true
                    }
                }) { Text(if (undoExpired) "거절 전송 중" else "거절 예정 · 되돌리기") }
            } else OutlinedButton(enabled = enabled, modifier = Modifier.testTag("review-reject"), onClick = {
                busy = true; onBusyChanged(true); error = null
                scope.launch {
                    val result = withContext(Dispatchers.IO) { runCatching { ReviewRuntime.scheduleReject(context, p) } }
                    result.onSuccess { pendingReject = it; onSubmitted() }.onFailure { error = it.message ?: "거절 예약 실패" }
                    busy = false
                    onBusyChanged(false)
                }
            }) { Text("거절") }
            Text("요약 · 바로 수정할 수 있습니다", style = MaterialTheme.typography.titleMedium)
            OutlinedTextField(value = body, onValueChange = { body = it }, enabled = enabled,
                label = { Text("요약 본문") }, modifier = Modifier.fillMaxWidth())
            if (!wide && p.sourceText.isNotBlank()) {
                TextButton(onClick = { showSource = !showSource }) { Text(if (showSource) "전체 원문 접기" else "전체 원문 보기") }
                if (showSource) Text(p.sourceText)
            }
            if (wikiLinks(body, vault).isNotEmpty()) {
                TextButton(onClick = { showLinks = !showLinks }) { Text("문서 링크 열기") }
                if (showLinks) {
                    OutlinedTextField(value = vault, onValueChange = {
                        vault = it; preferences.edit().putString("vault", it).apply()
                    }, label = { Text("Obsidian 볼트 이름") }, singleLine = true)
                    wikiLinks(body, vault).forEach { link -> WikiText(body.substring(link.start, link.end), vault) }
                }
            }
            ReviewCalendarSlot()
            record?.let {
                Text(it.status.reviewLabel())
                if (it.status in setOf(ReviewStatus.ERROR, ReviewStatus.PENDING))
                    OutlinedButton(onClick = { ReviewRuntime.retry(context) }) { Text("재시도") }
            }
            if (stale && record == null) Text("최신 문서를 다시 확인하세요")
            if (pendingReject != null && record == null) {
                Text("5초 뒤 답변 없이 거절을 보냅니다.", style = MaterialTheme.typography.bodySmall)
            } else if (!finalReview) {
                Text("확인할 질문", style = MaterialTheme.typography.titleMedium)
                if (p.questions.isEmpty()) Text("확인할 질문 없음")
                p.questions.forEachIndexed { index, q ->
                    Text(q.question)
                    val excerpt = q.sourceExcerpt.ifBlank { p.sourceText.take(1200) }
                    if (excerpt.isNotBlank()) {
                        Text("관련 원문", style = MaterialTheme.typography.labelLarge)
                        Surface(color = MaterialTheme.colorScheme.surfaceVariant, shape = MaterialTheme.shapes.small) {
                            Text(excerpt, modifier = Modifier.padding(12.dp))
                        }
                    } else Text("이전 문서에는 원문이 포함되지 않았습니다. 요약을 확인하고 답변해 주세요.",
                        style = MaterialTheme.typography.bodySmall)
                    q.choices.forEach { choice ->
                        val selected = answers.getOrNull(index) == choice
                        OutlinedButton(enabled = enabled, onClick = {
                            answers = ArrayList(answers).apply { set(index, choice) }
                        }) { Text(if (selected) "✓ $choice" else choice) }
                    }
                    OutlinedTextField(value = answers.getOrElse(index) { "" },
                        onValueChange = { value -> answers = ArrayList(answers).apply { set(index, value) } },
                        enabled = enabled, label = { Text("직접 답변 ${index + 1}") },
                        placeholder = { Text("선택지 대신 자유롭게 답변하세요") }, modifier = Modifier.fillMaxWidth())

                }
                OutlinedButton(enabled = enabled && answers.size == p.questions.size && answers.all { it.isNotBlank() },
                    onClick = { finalReview = true }) { Text("최종 검토") }
            } else {
                Text("최종 검토", style = MaterialTheme.typography.titleMedium)
                p.questions.forEachIndexed { index, q -> Text("${q.question}: ${answers.getOrElse(index) { "" }}") }
                OutlinedButton(enabled = enabled, onClick = { finalReview = false }) { Text("답변 수정") }
                OutlinedTextField(value = feedback, onValueChange = { feedback = it }, enabled = enabled,
                    label = { Text("추가 피드백 (선택)") }, modifier = Modifier.fillMaxWidth())
                Text("모든 답변과 피드백을 한 번에 보냅니다.", style = MaterialTheme.typography.bodySmall)
                run {
                    val action = if (body != p.draftBody) Verdict.Action.EDIT else Verdict.Action.APPROVE
                    OutlinedButton(enabled = enabled, onClick = {
                        busy = true; onBusyChanged(true); error = null
                        scope.launch {
                            val result = withContext(Dispatchers.IO) { runCatching {
                                val verdict = ReviewDraft.verdict(p, answers, action, body, feedback)
                                ReviewRuntime.submit(context, p, verdict)
                                ReviewRuntime.records(context).first { it.verdict == verdict }
                            } }
                            result.onSuccess { record = it; onSubmitted() }.onFailure { error = it.message ?: "전송 준비 실패" }
                            busy = false
                            onBusyChanged(false)
                        }
                    }) { Text(if (action == Verdict.Action.EDIT) "수정 제출" else "승인") }
                }
            }
        }
        if (record == null) readError?.let { Text(it) }
        error?.let { Text(it) }
    }
    if (wide && p != null) {
        // Summary, questions and actions on the left; the full source stays readable on the right.
        Row(Modifier.fillMaxSize(), horizontalArrangement = Arrangement.spacedBy(16.dp)) {
            Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp), content = content)
            Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(16.dp).testTag("review-source"), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("원문", style = MaterialTheme.typography.titleMedium)
                if (p.sourceText.isBlank()) Text("원문이 포함되지 않은 문서입니다", style = MaterialTheme.typography.bodySmall)
                else Text(p.sourceText)
            }
        }
    } else Column(verticalArrangement = Arrangement.spacedBy(8.dp), content = content)
}


@Suppress("DEPRECATION")
@Composable
private fun WikiText(text: String, vault: String) {
    val handler = LocalUriHandler.current
    val color = MaterialTheme.colorScheme.primary
    val annotated = remember(text, vault, color) {
        buildAnnotatedString {
            var offset = 0
            wikiLinks(text, vault).forEach { link ->
                append(text.substring(offset, link.start))
                val start = length
                append(link.label)
                link.uri?.let { uri ->
                    addStyle(SpanStyle(color = color, textDecoration = TextDecoration.Underline), start, length)
                    addStringAnnotation("wiki", uri, start, length)
                }
                offset = link.end
            }
            append(text.substring(offset))
        }
    }
    ClickableText(annotated, style = MaterialTheme.typography.bodyLarge, onClick = { position ->
        annotated.getStringAnnotations("wiki", position, position).firstOrNull()?.let {
            runCatching { handler.openUri(it.item) }
        }
    })
}
