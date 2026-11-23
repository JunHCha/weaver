package io.github.junhcha.weaver.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.schema.Evidence

/** What the evidence pane shows for the selected sentence. */
sealed interface EvidenceLoad {
    data object Loading : EvidenceLoad
    data object Unavailable : EvidenceLoad
    data class Ready(val evidence: Evidence) : EvidenceLoad
}

/**
 * Sentence -> the Threads that made it -> the whole source text. [onFeedback] opens the document's 피드백 tab for the
 * sentence; the button is disabled unless [feedbackEnabled] (the server offers feedback and the document is not a Digest).
 * The sheet wraps this on phones, the wide document pane shows it beside the document.
 */
@Composable
fun EvidenceSheet(evidence: Evidence, onFeedback: (String) -> Unit, feedbackEnabled: Boolean = false) {
    Column(
        Modifier.fillMaxWidth().testTag("evidence-sheet"),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text(evidence.text, style = MaterialTheme.typography.titleLarge, color = WeaverTokens.indigo)
        SectionHeading("이 문장을 만든 Thread")
        if (evidence.threads.isEmpty()) Muted("연결된 Thread가 없어요")
        evidence.threads.forEach { t ->
            Surface(
                Modifier.fillMaxWidth().semantics(mergeDescendants = true) {}.testTag("evidence-thread-${t.threadId}"),
                shape = RoundedCornerShape(WeaverTokens.number("radius.card").dp), color = WeaverTokens.surface,
                border = BorderStroke(1.dp, WeaverTokens.line),
            ) {
                Column(Modifier.padding(10.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                    t.content?.let { Text(it, style = MaterialTheme.typography.bodyLarge) }
                    Muted("${t.kind} · ${t.sourceLabel} · ${t.occurredAt.take(10)}")
                }
            }
        }
        evidence.sources.forEach { EvidenceSourceBlock(it) }
        TextButton(
            onClick = { onFeedback(evidence.bulletId) }, enabled = feedbackEnabled,
            modifier = Modifier.defaultMinSize(minHeight = 48.dp).testTag("evidence-feedback"),
        ) { Text("이 문장이 틀렸어요 → 피드백") }
    }
}

/** One whole source text under its heading; shared by the evidence sheet and the 로그 tab's 원문 보기. */
@Composable
internal fun EvidenceSourceBlock(s: io.github.junhcha.weaver.schema.EvidenceSource) {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        SectionHeading("원문 · ${s.label}")
        Muted(listOfNotNull(s.channel, s.occurredAt?.take(10)).joinToString(" · ").ifEmpty { " " })
        Surface(
            Modifier.fillMaxWidth().semantics(mergeDescendants = true) {}.testTag("evidence-source-${s.sourceId}"),
            shape = RoundedCornerShape(WeaverTokens.number("radius.card").dp), color = WeaverTokens.surface,
            border = BorderStroke(1.dp, WeaverTokens.line),
        ) {
            Text(
                s.text, Modifier.heightIn(max = 280.dp).verticalScroll(rememberScrollState()).padding(10.dp),
                style = MaterialTheme.typography.bodyMedium,
            )
        }
        if (s.truncated) Muted("원문이 길어 앞부분만 보여줘요")
    }
}

/** Placeholder body while the evidence loads or cannot be read. */
@Composable
internal fun EvidenceStatus(load: EvidenceLoad, onFeedback: (String) -> Unit, feedbackEnabled: Boolean = false) {
    when (load) {
        EvidenceLoad.Loading -> Muted("근거를 불러오는 중이에요", Modifier.testTag("evidence-loading"))
        EvidenceLoad.Unavailable -> Muted("지금은 근거를 불러올 수 없어요. 연결을 확인해 주세요.", Modifier.testTag("evidence-unavailable"))
        is EvidenceLoad.Ready -> EvidenceSheet(load.evidence, onFeedback, feedbackEnabled)
    }
}

/** Phone layout: the evidence in a bottom sheet over the document. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun EvidenceBottomSheet(load: EvidenceLoad, onFeedback: (String) -> Unit, dismiss: () -> Unit, feedbackEnabled: Boolean = false) {
    ModalBottomSheet(onDismissRequest = dismiss) {
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            EvidenceStatus(load, onFeedback, feedbackEnabled)
            TextButton(onClick = dismiss, Modifier.defaultMinSize(minHeight = 48.dp)) { Text("닫기") }
        }
    }
}
