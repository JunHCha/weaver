package io.github.junhcha.weaver.ui

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.relocation.BringIntoViewRequester
import androidx.compose.foundation.relocation.bringIntoViewRequester
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.produceState
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.phone.Cached
import io.github.junhcha.weaver.phone.ResultClient
import io.github.junhcha.weaver.schema.DocBullet
import io.github.junhcha.weaver.schema.DocSection
import io.github.junhcha.weaver.schema.DocSectionKind
import io.github.junhcha.weaver.schema.DocumentKind
import io.github.junhcha.weaver.schema.DocumentView
import io.github.junhcha.weaver.schema.RefLinkView
import io.github.junhcha.weaver.schema.TaskItem

/** Reading order of the sections whatever order the server sent them in (mockup v3). */
private fun DocSectionKind.rank() = when (this) {
    DocSectionKind.SUMMARY, DocSectionKind.CURRENT -> 0
    DocSectionKind.TASKS, DocSectionKind.REMAINING -> 1
    DocSectionKind.WAITING -> 2
    DocSectionKind.CAUTIONS -> 3
}

private fun DocBullet.matches(target: String?) = target != null && (bulletId == target || task?.blockId == target)

/**
 * A compiled document in the mockup's order: title, "as_of 기준", 현재 상태, 남은 일, 기다리는 중, 주의할 이력, 관련 자료 and the
 * link to the log. A bullet opens its evidence; a task line reports [onTask]. [focus] (a bullet or block id) is scrolled
 * into view and highlighted. Sections without lines are left out. The time thread of 주의할 이력 is the only 홍화 here.
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun DocumentScreen(
    view: DocumentView,
    onEvidence: (String) -> Unit,
    onTask: (TaskItem) -> Unit,
    onLog: (String) -> Unit,
    focus: String?,
    extraReferences: List<RefLinkView> = emptyList(),
    modifier: Modifier = Modifier,
    referencesStale: Boolean = false,
    /** Thread ids of the tasks checked on this phone: their lines show as done. */
    doneIds: Set<String> = emptySet(),
) {
    val sections = remember(view) { view.sections.filter { it.bullets.isNotEmpty() }.sortedBy { it.kind.rank() } }
    val references = remember(view, extraReferences) { (view.references + extraReferences).distinct() }
    Column(
        modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp, vertical = 8.dp).testTag("document-screen"),
        verticalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        Text(view.title, style = MaterialTheme.typography.titleLarge, color = WeaverTokens.indigo)
        val subtitle = listOfNotNull(view.asOf?.let { "$it 기준" }, if (view.reflecting) "반영중" else null).joinToString(" · ")
        if (subtitle.isNotEmpty()) Muted(subtitle, Modifier.testTag("document-subtitle"))
        sections.forEach { section ->
            SectionHeading(section.title, emphasis = section.kind == DocSectionKind.CURRENT || section.kind == DocSectionKind.SUMMARY)
            if (section.kind == DocSectionKind.CAUTIONS) CautionLines(section, focus, onEvidence)
            else section.bullets.forEach { BulletRow(it, focus, doneIds, onEvidence, onTask) }
        }
        if (references.isNotEmpty()) {
            SectionHeading("관련 자료")
            if (referencesStale) Muted("저장된 사본이에요", Modifier.testTag("document-references-stale"))
            references.forEach { r ->
                Column(Modifier.fillMaxWidth().padding(vertical = 6.dp).testTag("doc-reference-${r.path}")) {
                    Text(r.heading, style = MaterialTheme.typography.bodyLarge)
                    Muted(listOf(r.sphere, r.path).plus(if (r.home) listOf("홈") else emptyList()).joinToString(" · "))
                }
            }
        }
        view.logSubject?.let { subject ->
            TextButton(onClick = { onLog(subject) }, Modifier.defaultMinSize(minHeight = 48.dp).testTag("document-log-link")) {
                Text("로그에서 시간순으로 보기 →")
            }
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun BulletRow(bullet: DocBullet, focus: String?, doneIds: Set<String>, onEvidence: (String) -> Unit, onTask: (TaskItem) -> Unit) {
    val focused = bullet.matches(focus)
    val requester = remember { BringIntoViewRequester() }
    if (focused) LaunchedEffect(focus) { requester.bringIntoView() }
    val task = bullet.task
    val checked = task != null && task.threadId in doneIds
    Row(
        Modifier.fillMaxWidth().defaultMinSize(minHeight = 48.dp)
            .bringIntoViewRequester(requester)
            .background(if (focused) WeaverTokens.selected else androidx.compose.ui.graphics.Color.Transparent)
            .clickable(role = Role.Button) { onEvidence(bullet.bulletId) }
            .semantics { if (focused) stateDescription = "강조됨" }
            .padding(vertical = 8.dp, horizontal = 4.dp)
            .testTag("doc-bullet-${bullet.bulletId}"),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (task != null) Box(
            Modifier.size(48.dp)
                .toggleable(value = checked, role = Role.Checkbox, onValueChange = { if (!checked) onTask(task) })
                .semantics { stateDescription = if (checked) "완료로 기록함" else "완료 안 됨"; contentDescription = "${bullet.text} 완료로 기록" }
                .testTag("doc-task-check-${bullet.bulletId}"),
            contentAlignment = Alignment.Center,
        ) { Text(if (checked) "☑" else "☐", Modifier.clearAndSetSemantics {}, style = MaterialTheme.typography.bodyLarge, color = WeaverTokens.muted) }
        else Text("•", Modifier.padding(end = 8.dp).clearAndSetSemantics {}, style = MaterialTheme.typography.bodyLarge, color = WeaverTokens.muted)
        Text(bullet.text, Modifier.weight(1f), style = MaterialTheme.typography.bodyLarge,
            textDecoration = if (checked) androidx.compose.ui.text.style.TextDecoration.LineThrough else null)
        bullet.label?.let { Muted(it, Modifier.padding(start = 8.dp)) }
    }
}

/** 주의할 이력: dated lines on a vertical thread; the thread and its knots are 홍화. */
@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun CautionLines(section: DocSection, focus: String?, onEvidence: (String) -> Unit) {
    val thread = WeaverTokens.thread
    val line = WeaverTokens.number("stroke.weftIdle")
    Column(
        Modifier.fillMaxWidth().drawBehind {
            val x = 8.dp.toPx()
            drawLine(thread, Offset(x, 0f), Offset(x, size.height), strokeWidth = line.dp.toPx())
        }.testTag("doc-caution-thread"),
    ) {
        section.bullets.forEach { b ->
            val focused = b.matches(focus)
            val requester = remember { BringIntoViewRequester() }
            if (focused) LaunchedEffect(focus) { requester.bringIntoView() }
            Row(
                Modifier.fillMaxWidth().height(IntrinsicSize.Min).defaultMinSize(minHeight = 48.dp).bringIntoViewRequester(requester)
                    .background(if (focused) WeaverTokens.selected else androidx.compose.ui.graphics.Color.Transparent)
                    .clickable(role = Role.Button) { onEvidence(b.bulletId) }
                    .semantics { if (focused) stateDescription = "강조됨" }
                    .testTag("doc-bullet-${b.bulletId}"),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Box(Modifier.width(16.dp).fillMaxHeight().drawBehind {
                    drawCircle(thread, radius = 4.dp.toPx(), center = Offset(8.dp.toPx(), size.height / 2))
                })
                b.day?.let { Muted(it.drop(5).replaceFirst('-', '/'), Modifier.padding(end = 8.dp)) }
                Text(b.text, Modifier.weight(1f).padding(vertical = 8.dp), style = MaterialTheme.typography.bodyLarge)
            }
        }
    }
}

/** `문서 | 피드백` (wide: `근거 | 피드백`): two tabs that switch what is shown; each is a 48dp target with its state read out. */
@Composable
private fun DocumentSegments(feedback: Boolean, first: String, onFeedback: (Boolean) -> Unit, modifier: Modifier = Modifier) {
    Row(modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp).testTag("document-segments"), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        listOf(false to first, true to "피드백").forEach { (isFeedback, label) ->
            val on = feedback == isFeedback
            Box(
                Modifier.weight(1f).defaultMinSize(minHeight = 48.dp)
                    .background(if (on) WeaverTokens.selected else androidx.compose.ui.graphics.Color.Transparent)
                    .clickable(role = Role.Tab) { onFeedback(isFeedback) }
                    .semantics { selected = on; stateDescription = if (on) "선택됨" else "선택 안 됨" }
                    .testTag(if (isFeedback) "document-segment-feedback" else "document-segment-document"),
                contentAlignment = Alignment.Center,
            ) { Text(label, style = if (on) MaterialTheme.typography.labelLarge else MaterialTheme.typography.labelMedium, color = if (on) WeaverTokens.indigo else WeaverTokens.muted) }
        }
    }
}

/**
 * The document with its evidence and its 피드백 tab. Phones show the evidence in a bottom sheet and the tab in place of the
 * document; a [wide] layout splits the pane into document | right pane, where 근거 and 피드백 switch. [bulletId] is the
 * selected sentence; its evidence is read from the server when it changes. The tab exists only when the server offers
 * `features.feedback` and the document is not a Digest. [feedbackFirst] (an overwrite alert) opens it right away.
 */
@Composable
fun DocumentPane(
    view: DocumentView,
    results: ResultClient,
    bulletId: String?,
    onBullet: (String?) -> Unit,
    onTask: (TaskItem) -> Unit,
    onLog: (String) -> Unit,
    focus: String?,
    wide: Boolean,
    extraReferences: List<RefLinkView> = emptyList(),
    referencesStale: Boolean = false,
    doneIds: Set<String> = emptySet(),
    feedbackFirst: Boolean = false,
    reload: Int = 0,
    feedbackKey: () -> String = { java.util.UUID.randomUUID().toString() },
) {
    val load by produceState<EvidenceLoad>(EvidenceLoad.Loading, view.documentId, bulletId) {
        value = EvidenceLoad.Loading
        if (bulletId != null) value = results.evidence(view.documentId, bulletId)?.let { EvidenceLoad.Ready(it) } ?: EvidenceLoad.Unavailable
    }
    val feedbackOn by produceState(false, results) { value = results.feedbackEnabled() }
    val canFeedback = feedbackOn && view.kind != DocumentKind.DIGEST
    var feedback by rememberSaveable(view.documentId) { mutableStateOf(feedbackFirst) }
    var feedbackBullet by rememberSaveable(view.documentId) { mutableStateOf<String?>(null) }
    LaunchedEffect(feedbackFirst) { if (feedbackFirst) feedback = true }
    val feedbackShown = canFeedback && feedback
    // The sentence's evidence closes and its feedback opens, quoting the sentence.
    val wrong: (String) -> Unit = { id -> feedbackBullet = id; feedback = true; onBullet(null) }
    val feedbackPane: @Composable (Modifier) -> Unit = { m ->
        FeedbackPane(
            view.documentId, feedbackBullet, results, m, view.sections.flatMap { it.bullets }.firstOrNull { it.bulletId == feedbackBullet }?.text, reload, feedbackKey, { feedbackBullet = null },
        )
    }
    val document: @Composable (Modifier) -> Unit = { m ->
        DocumentScreen(view, { onBullet(it) }, onTask, onLog, focus, extraReferences, m, referencesStale, doneIds)
    }
    if (wide) {
        Row(Modifier.fillMaxSize()) {
            Box(Modifier.weight(1f).fillMaxHeight()) { document(Modifier) }
            Box(Modifier.width(1.dp).fillMaxHeight().background(WeaverTokens.line))
            Column(Modifier.weight(1f).fillMaxHeight().testTag("document-evidence-pane")) {
                if (canFeedback) DocumentSegments(feedbackShown, "근거", { feedbackBullet = null; feedback = it })
                Box(Modifier.weight(1f).fillMaxWidth()) {
                    if (feedbackShown) feedbackPane(Modifier)
                    else if (bulletId == null) Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { Muted("문장을 누르면 근거가 보여요") }
                    else Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp)) {
                        EvidenceStatus(load, wrong, canFeedback)
                        TextButton(onClick = { onBullet(null) }, Modifier.defaultMinSize(minHeight = 48.dp)) { Text("닫기") }
                    }
                }
            }
        }
    } else {
        Column(Modifier.fillMaxSize()) {
            if (canFeedback) DocumentSegments(feedbackShown, "문서", { feedbackBullet = null; feedback = it })
            Box(Modifier.weight(1f).fillMaxWidth()) { if (feedbackShown) feedbackPane(Modifier) else document(Modifier) }
        }
        if (bulletId != null && !feedbackShown) EvidenceBottomSheet(load, wrong, { onBullet(null) }, canFeedback)
    }
}

/** Loads [documentId] (cached, offline-capable) and the references of [referencePath], then shows the [DocumentPane]. */
@Composable
fun DocumentRoute(
    results: ResultClient,
    documentId: String,
    referencePath: String?,
    bulletId: String?,
    onBullet: (String?) -> Unit,
    onTask: (TaskItem) -> Unit,
    onLog: (String) -> Unit,
    focus: String?,
    wide: Boolean,
    onBack: () -> Unit,
    reload: Int = 0,
    doneIds: Set<String> = emptySet(),
    feedbackFirst: Boolean = false,
) {
    val loaded by produceState<DocumentLoad>(DocumentLoad.Loading, results, documentId, reload) {
        value = results.document(documentId)?.let { DocumentLoad.Ready(it) } ?: DocumentLoad.Missing
    }
    val references by produceState<Cached<List<RefLinkView>>?>(null, results, referencePath) {
        value = referencePath?.let { results.references(it) }
    }
    Column(Modifier.fillMaxSize()) {
        Row(Modifier.fillMaxWidth().padding(end = 16.dp), verticalAlignment = Alignment.CenterVertically) {
            TextButton(
                onClick = onBack,
                Modifier.defaultMinSize(minWidth = 48.dp, minHeight = 48.dp).semantics { contentDescription = "뒤로" }.testTag("document-back"),
            ) { Text("‹") }
            (loaded as? DocumentLoad.Ready)?.cached?.offlineSince?.let { Muted("오프라인 · 저장된 사본", Modifier.testTag("document-offline")) }
        }
        Box(Modifier.weight(1f)) {
            when (val state = loaded) {
                DocumentLoad.Loading -> Muted("문서를 불러오는 중이에요", Modifier.padding(16.dp))
                DocumentLoad.Missing -> Muted("문서를 불러오지 못했어요. 연결을 확인해 주세요.", Modifier.padding(16.dp).testTag("document-missing"))
                is DocumentLoad.Ready -> DocumentPane(state.cached.value, results, bulletId, onBullet, onTask, onLog, focus, wide, references?.value.orEmpty(), references?.offlineSince != null, doneIds, feedbackFirst, reload)
            }
        }
    }
}

private sealed interface DocumentLoad {
    data object Loading : DocumentLoad
    data object Missing : DocumentLoad
    data class Ready(val cached: Cached<DocumentView>) : DocumentLoad
}
