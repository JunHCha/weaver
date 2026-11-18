package io.github.junhcha.weaver.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.phone.Direction
import io.github.junhcha.weaver.phone.Membership
import io.github.junhcha.weaver.phone.PhoneSnapshot
import io.github.junhcha.weaver.phone.PhoneTag
import io.github.junhcha.weaver.phone.TagKind
import io.github.junhcha.weaver.phone.TimelineRow
import io.github.junhcha.weaver.phone.areaDirectory
import io.github.junhcha.weaver.phone.partyDirectory
import io.github.junhcha.weaver.phone.timeline

/** First level of the 사람 / 영역 tabs: pick one subject. */
@Composable
internal fun SubjectList(title: String, entries: List<Pair<PhoneTag, Int>>, emptyText: String, onSelect: (String) -> Unit) {
    Column(Modifier.fillMaxSize().testTag("subject-list")) {
        ScreenTitle(title)
        LazyColumn(Modifier.weight(1f), contentPadding = PaddingValues(horizontal = 16.dp)) {
            if (entries.isEmpty()) item { Muted(emptyText) }
            items(entries, key = { it.first.name }) { (tag, count) ->
                Row(
                    Modifier.fillMaxWidth().heightIn(min = 48.dp).clickable { onSelect(tag.name) }.padding(vertical = 12.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(tag.name, Modifier.weight(1f), style = MaterialTheme.typography.bodyLarge)
                    if (tag.kind == TagKind.ORGANIZATION) Muted("조직 · ")
                    Muted("${count}건")
                }
            }
        }
    }
}

internal fun peopleEntries(snapshot: PhoneSnapshot) = partyDirectory(snapshot)

internal fun areaEntries(snapshot: PhoneSnapshot) = areaDirectory(snapshot)

/**
 * One person, organization or area as a single time-ordered column. People show counterpart threads only by
 * default; "언급 포함" adds mention-only threads dimmed with a 언급 badge; "이력 포함" follows the web fact/history rule.
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
internal fun TimelineScreen(
    snapshot: PhoneSnapshot, subject: String, onBack: () -> Unit,
    tagHost: TagEditHost? = null, onEdited: () -> Unit = {},
) {
    val isParty = snapshot.tags.firstOrNull { it.name == subject }?.kind?.isParty == true
    var mentions by rememberSaveable(subject) { mutableStateOf(false) }
    var history by rememberSaveable(subject) { mutableStateOf(false) }
    val rows = remember(snapshot, subject, mentions, history) { timeline(snapshot, subject, mentions, history) }
    // Tag editing: a tap opens the sheet for one Thread; a long press starts a multi-selection for batch edits.
    var selection by remember(subject) { mutableStateOf<Set<String>>(emptySet()) }
    var sheet by remember(subject) { mutableStateOf<List<String>?>(null) }
    var dictionary by remember(subject) { mutableStateOf(false) }
    val byId = remember(rows) { rows.associate { it.thread.id to it.thread } }
    Column(Modifier.fillMaxSize().testTag("timeline-screen")) {
        Row(Modifier.fillMaxWidth().padding(end = 16.dp), verticalAlignment = Alignment.CenterVertically) {
            TextButton(
                onClick = onBack,
                Modifier.defaultMinSize(minWidth = 48.dp, minHeight = 48.dp).semantics { contentDescription = "뒤로" },
            ) { Text("‹") }
            Text(subject, style = MaterialTheme.typography.titleLarge, color = WeaverTokens.indigo)
            val kind = snapshot.tags.firstOrNull { it.name == subject }?.kind
            if (kind != null) Muted(kind.label, Modifier.padding(start = 8.dp))
            if (tagHost != null) {
                androidx.compose.foundation.layout.Spacer(Modifier.weight(1f))
                TextButton(onClick = { dictionary = true }, Modifier.defaultMinSize(minHeight = 48.dp).testTag("dictionary-open")) { Text("사전") }
            }
        }
        if (selection.isNotEmpty()) Row(
            Modifier.fillMaxWidth().padding(horizontal = 16.dp), verticalAlignment = Alignment.CenterVertically,
        ) {
            Text("${selection.size}개 선택", Modifier.weight(1f), style = MaterialTheme.typography.labelLarge)
            TextButton(onClick = { selection = emptySet() }, Modifier.defaultMinSize(minHeight = 48.dp)) { Text("취소") }
            TextButton(onClick = { sheet = selection.toList() }, Modifier.defaultMinSize(minHeight = 48.dp).testTag("batch-edit")) { Text("태그 편집") }
        }
        Row(Modifier.padding(horizontal = 16.dp, vertical = 6.dp), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            if (isParty) {
                PhoneChip("상대", selected = true, onClick = null)
                PhoneChip("언급 포함", mentions, { mentions = !mentions })
            }
            PhoneChip("이력 포함", history, { history = !history })
        }
        if (rows.isEmpty()) Muted("보여 줄 Thread가 없어요", Modifier.padding(16.dp))
        LazyColumn(Modifier.weight(1f), contentPadding = PaddingValues(horizontal = 16.dp)) {
            items(rows, key = { it.thread.id }) { row ->
                val id = row.thread.id
                TimelineEntry(
                    row, selected = id in selection,
                    modifier = if (tagHost == null) Modifier else Modifier.combinedClickable(
                        onLongClick = { selection = selection + id },
                        onClick = { if (selection.isNotEmpty()) selection = if (id in selection) selection - id else selection + id else sheet = listOf(id) },
                    ).testTag("thread-$id"),
                )
            }
        }
    }
    val editing = sheet
    if (tagHost != null && editing != null) {
        TagEditSheet(editing.mapNotNull { byId[it] }, snapshot.tags, tagHost) { changed ->
            sheet = null; selection = emptySet()
            if (changed) onEdited()
        }
    }
    if (tagHost != null && dictionary) {
        DictionarySheet(subject, snapshot.tags, tagHost) { changed -> dictionary = false; if (changed) onEdited() }
    }
}

@Composable
private fun TimelineEntry(row: TimelineRow, selected: Boolean = false, modifier: Modifier = Modifier) {
    val t = row.thread
    val mention = row.membership == Membership.MENTION
    val dateLabel = "${t.occurredAt.shortDate()} · ${t.source}"
    Row(modifier.height(IntrinsicSize.Min).padding(bottom = 8.dp)) {
        // The only red on the phone: the time thread and its knots.
        Canvas(Modifier.width(14.dp).fillMaxHeight()) {
            val x = size.width / 2
            drawLine(WeaverTokens.thread.copy(alpha = 0.85f), Offset(x, 0f), Offset(x, size.height), 2.dp.toPx())
            drawCircle(WeaverTokens.bg, 5.dp.toPx(), Offset(x, 10.dp.toPx()))
            drawCircle(WeaverTokens.thread, 5.dp.toPx(), Offset(x, 10.dp.toPx()), style = Stroke(2.dp.toPx()))
        }
        Column(Modifier.padding(start = 8.dp)) {
            Muted(dateLabel)
            ThreadCard(
                t,
                detail = listOfNotNull(
                    t.direction?.let { if (it == Direction.RECEIVED) "받은 요청" else "보낸 요청" },
                    t.deadline?.let { "마감 ${it.shortDate()}" },
                ).joinToString(" · "),
                muted = mention,
                badge = when {
                    selected -> "✓ 선택"
                    mention -> "언급"
                    row.isHistory -> "이력"
                    else -> null
                },
            )
        }
    }
}

