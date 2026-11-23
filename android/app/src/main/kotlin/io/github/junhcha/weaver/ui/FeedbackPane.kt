package io.github.junhcha.weaver.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.phone.FeedbackOutcome
import io.github.junhcha.weaver.phone.ResultClient
import io.github.junhcha.weaver.schema.FeedbackChip
import io.github.junhcha.weaver.schema.FeedbackItem
import io.github.junhcha.weaver.schema.FeedbackStatus
import io.github.junhcha.weaver.schema.label
import java.util.UUID
import kotlinx.coroutines.launch

internal const val FEEDBACK_HINT = "이 문서가 틀렸거나 맥락이 어긋났으면 말로 알려 주세요. 새 Thread로 기록되고 문서가 다시 만들어집니다."

/** What a feedback line says about its state; a pending note waits for the Mac, an applied one reports what was changed. */
internal fun FeedbackItem.statusLine(): String = when (status) {
    FeedbackStatus.PENDING -> "반영 중 · Mac 처리 대기"
    FeedbackStatus.APPLIED -> result?.takeIf { it.isNotBlank() } ?: "반영됨"
    FeedbackStatus.FAILED -> "처리하지 못했어요"
    FeedbackStatus.UNDONE -> "되돌림"
}

/** A stable server code as words the person can act on; anything unknown gets one generic line. */
internal fun feedbackProblem(code: String): String = when (code) {
    "document_not_found" -> "이 문서를 찾을 수 없어요. 문서가 바뀌었는지 확인해 주세요."
    "feedback_already_undone" -> "이미 되돌린 피드백이에요."
    "feedback_not_found" -> "피드백을 찾을 수 없어요."
    "invalid_request" -> "내용을 보낼 수 없어요. 글자 수를 확인해 주세요."
    "scope_denied", "invalid_token" -> "이 기기에서는 피드백을 보낼 수 없어요. 연결 설정을 확인해 주세요."
    else -> "처리하지 못했어요. 잠시 뒤 다시 시도해 주세요."
}

/** Adds `[chip] ` after what is typed (one space between), the way the chip reads in the note. */
private fun withChip(text: String, chip: FeedbackChip): String =
    text + (if (text.isEmpty() || text.last().isWhitespace()) "" else " ") + "[${chip.label}] "

private fun withoutChip(text: String, chip: FeedbackChip): String = text.replace("[${chip.label}] ", "").replace("[${chip.label}]", "")

/**
 * The 피드백 tab of a document: say in words what is wrong, send it, and see what the Mac did with each note. A note
 * becomes a new Thread and the document is compiled again; it never completes a task. [bulletId] (with its sentence in
 * [bulletText]) quotes the sentence the note is about. The `client_id` is made per input and kept while the same input is
 * sent again, so a lost response never records it twice. [reload] refreshes the list when it changes.
 */
@OptIn(androidx.compose.foundation.layout.ExperimentalLayoutApi::class)
@Composable
fun FeedbackPane(
    documentId: String,
    bulletId: String?,
    client: ResultClient,
    modifier: Modifier = Modifier,
    bulletText: String? = null,
    reload: Int = 0,
    newKey: () -> String = { UUID.randomUUID().toString() },
    /** The quoted sentence is no longer the target (sent, or removed with the ×): the owner forgets [bulletId]. */
    onBulletCleared: () -> Unit = {},
) {
    val scope = rememberCoroutineScope()
    var text by rememberSaveable(documentId) { mutableStateOf("") }
    var chipBits by rememberSaveable(documentId) { mutableIntStateOf(0) }
    // The key of the input that was sent last without an answer: same input -> same key.
    var keyed by rememberSaveable(documentId) { mutableStateOf("") }
    var key by rememberSaveable(documentId) { mutableStateOf("") }
    var items by remember(documentId) { mutableStateOf<List<FeedbackItem>?>(null) }
    var loadFailed by remember(documentId) { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var refreshes by remember { mutableIntStateOf(0) }
    val undoKeys = remember(documentId) { mutableMapOf<String, String>() }

    LaunchedEffect(documentId, reload, refreshes) {
        val loaded = client.feedback(documentId)
        if (loaded != null) items = loaded.sortedByDescending { it.createdAt }
        loadFailed = loaded == null
    }
    fun merge(item: FeedbackItem) {
        items = (listOf(item) + items.orEmpty().filter { it.feedbackId != item.feedbackId }).sortedByDescending { it.createdAt }
    }
    val chips = FeedbackChip.entries.filter { chipBits and (1 shl it.ordinal) != 0 }
    val body = text.trim()

    Column(
        modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp, vertical = 8.dp).testTag("feedback-pane"),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Muted(FEEDBACK_HINT, Modifier.testTag("feedback-hint"))
        if (bulletId != null) Surface(
            Modifier.fillMaxWidth().semantics(mergeDescendants = true) {}.testTag("feedback-quote"),
            shape = RoundedCornerShape(WeaverTokens.number("radius.card").dp), color = WeaverTokens.surface,
            border = BorderStroke(1.dp, WeaverTokens.line),
        ) {
            Row(Modifier.padding(start = 10.dp), verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
                Column(Modifier.weight(1f).padding(vertical = 10.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                    Muted("이 문장에 대해")
                    Text(bulletText ?: "선택한 문장", style = MaterialTheme.typography.bodyLarge)
                }
                TextButton(
                    onClick = onBulletCleared,
                    Modifier.defaultMinSize(minWidth = 48.dp, minHeight = 48.dp)
                        .semantics { contentDescription = "문장 인용 지우기" }.testTag("feedback-quote-clear"),
                ) { Text("×") }
            }
        }
        OutlinedTextField(
            value = text, onValueChange = { text = it }, enabled = !busy,
            placeholder = { Text("예: 견적 회신은 김철수가 아니라 홍길동 담당이에요") },
            modifier = Modifier.fillMaxWidth().defaultMinSize(minHeight = 96.dp).testTag("feedback-input"),
        )
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FeedbackChip.entries.forEach { chip ->
                val on = chip in chips
                PhoneChip(chip.label, on, {
                    chipBits = chipBits xor (1 shl chip.ordinal)
                    text = if (on) withoutChip(text, chip).trimEnd() else withChip(text, chip)
                }, Modifier.testTag("feedback-chip-${chip.name}"))
            }
        }
        error?.let { Muted(it, Modifier.testTag("feedback-error")) }
        TextButton(
            onClick = {
                val fingerprint = "$bulletId|$body|$chipBits"
                if (keyed != fingerprint) { keyed = fingerprint; key = newKey() }
                val clientId = key
                busy = true; error = null
                scope.launch {
                    when (val outcome = client.sendFeedback(documentId, bulletId, body, chips, clientId)) {
                        is FeedbackOutcome.Done -> { merge(outcome.item); text = ""; chipBits = 0; keyed = ""; key = ""; onBulletCleared() }
                        is FeedbackOutcome.Rejected -> { error = feedbackProblem(outcome.code); keyed = ""; key = "" }
                        FeedbackOutcome.Unreachable -> error = "보내지 못했어요. 연결을 확인하고 다시 눌러 주세요."
                    }
                    busy = false
                }
            },
            enabled = body.isNotEmpty() && !busy,
            modifier = Modifier.defaultMinSize(minWidth = 48.dp, minHeight = 48.dp).testTag("feedback-send"),
        ) { Text("보내기") }

        Row(Modifier.fillMaxWidth(), verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
            SectionHeading("이 문서에 보낸 피드백")
            Row(Modifier.weight(1f), horizontalArrangement = Arrangement.End) {
                TextButton(
                    onClick = { refreshes++ },
                    Modifier.defaultMinSize(minWidth = 48.dp, minHeight = 48.dp).testTag("feedback-refresh"),
                ) { Text("새로고침") }
            }
        }
        val shown = items
        when {
            shown == null && loadFailed -> Muted("목록을 불러오지 못했어요. 연결을 확인해 주세요.", Modifier.testTag("feedback-unavailable"))
            shown == null -> Muted("불러오는 중이에요")
            shown.isEmpty() -> Muted("아직 보낸 피드백이 없어요", Modifier.testTag("feedback-empty"))
            else -> shown.forEach { item ->
                FeedbackRow(item, busy = busy) {
                    val clientId = undoKeys.getOrPut(item.feedbackId, newKey)
                    busy = true; error = null
                    scope.launch {
                        when (val outcome = client.undoFeedback(item.feedbackId, clientId)) {
                            is FeedbackOutcome.Done -> { merge(outcome.item); undoKeys.remove(item.feedbackId) }
                            is FeedbackOutcome.Rejected -> { undoKeys.remove(item.feedbackId); refreshes++; error = feedbackProblem(outcome.code) }
                            FeedbackOutcome.Unreachable -> error = "되돌리지 못했어요. 연결을 확인하고 다시 눌러 주세요."
                        }
                        busy = false
                    }
                }
            }
        }
    }
}

@Composable
private fun FeedbackRow(item: FeedbackItem, busy: Boolean, onUndo: () -> Unit) {
    val undoable = item.status == FeedbackStatus.PENDING || item.status == FeedbackStatus.APPLIED
    Surface(
        Modifier.fillMaxWidth().testTag("feedback-item-${item.feedbackId}"),
        shape = RoundedCornerShape(WeaverTokens.number("radius.card").dp), color = WeaverTokens.surface,
        border = BorderStroke(1.dp, WeaverTokens.line),
    ) {
        Column(Modifier.padding(10.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Text(item.text, style = MaterialTheme.typography.bodyLarge)
            if (item.chips.isNotEmpty()) Muted(item.chips.joinToString(" · ") { it.label })
            Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
                Text(
                    item.statusLine(), Modifier.weight(1f).testTag("feedback-status-${item.feedbackId}"),
                    style = MaterialTheme.typography.labelMedium,
                    color = if (item.status == FeedbackStatus.APPLIED) WeaverTokens.indigo else WeaverTokens.muted,
                )
                if (undoable) TextButton(
                    onClick = onUndo, enabled = !busy,
                    modifier = Modifier.defaultMinSize(minWidth = 48.dp, minHeight = 48.dp)
                        .semantics { stateDescription = if (item.status == FeedbackStatus.PENDING) "반영 전" else "반영됨" }
                        .testTag("feedback-undo-${item.feedbackId}"),
                ) { Text("되돌리기") }
            }
        }
    }
}
