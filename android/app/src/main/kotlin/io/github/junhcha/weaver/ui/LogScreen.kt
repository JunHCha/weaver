package io.github.junhcha.weaver.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.phone.PhoneThread
import io.github.junhcha.weaver.phone.ResultClient
import io.github.junhcha.weaver.schema.SourceLogEntry
import io.github.junhcha.weaver.schema.TreeNodeView
import io.github.junhcha.weaver.schema.UsedIn
import io.github.junhcha.weaver.schema.UsedInKind
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale
import kotlinx.coroutines.launch

/** 로그 > 전체: every Thread the phone knows, newest occurrence first. */
@Composable
internal fun LogThreads(threads: List<PhoneThread>, emptyText: String, zone: ZoneId, onThread: (PhoneThread) -> Unit, modifier: Modifier = Modifier) {
    val ordered = remember(threads) { threads.sortedWith(compareByDescending<PhoneThread> { it.occurredAt }.thenByDescending { it.seq }) }
    LazyColumn(
        modifier.testTag("log-threads"), contentPadding = PaddingValues(horizontal = 16.dp, vertical = 4.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        if (ordered.isEmpty()) item { Muted(emptyText) }
        items(ordered, key = { it.id }) { thread ->
            val at = thread.occurredAt.atZone(zone)
            ThreadCard(
                thread, "${at.toLocalDate().shortDate()} ${"%02d:%02d".format(at.hour, at.minute)} · ${thread.kind.label} · ${thread.source}",
                onClick = { onThread(thread) },
            )
        }
    }
}

/** The 로그 tab's filters; each one is sent to `/read/sources` as the query of the same name. */
internal data class SourceFilters(val sphere: String? = null, val person: String? = null, val kind: String? = null) {
    val query: Map<String, String> get() = buildMap {
        sphere?.let { put("sphere", it) }
        person?.let { put("person", it) }
        kind?.let { put("kind", it) }
    }
}

/** Kinds the server can filter by, in the order the 종류 sheet lists them. */
internal val SourceKinds = listOf("요청", "결정", "일정", "기록")

/**
 * Where "로그에서 시간순으로 보기" of a document leads. The sources API filters by 생활권 (the tree root) or by person, not by
 * document: a subject that is a tree node filters by its root, any other subject is a person.
 */
internal fun logFiltersFor(subject: String, nodes: List<TreeNodeView>): SourceFilters =
    if (nodes.any { it.path == subject }) SourceFilters(sphere = subject.substringBefore('/')) else SourceFilters(person = subject)

/** The tree node a link names: by path first, then by label, then by the last path segment. */
internal fun areaNodeFor(name: String, nodes: List<TreeNodeView>): TreeNodeView? =
    nodes.firstOrNull { it.path == name } ?: nodes.firstOrNull { it.label == name }
        ?: nodes.firstOrNull { it.path.substringAfterLast('/') == name }

private data class SourcePaging(val items: List<SourceLogEntry> = emptyList(), val next: String? = null, val loaded: Boolean = false, val failed: Boolean = false)

private val dayFormat = DateTimeFormatter.ofPattern("M월 d일 E", Locale.KOREAN)

/** A Digest's title is its date (`2026-11-12 (목)`): the used-in link reads `11/12 Digest`. */
internal fun usedInLabel(used: UsedIn): String = when (used.kind) {
    UsedInKind.DOCUMENT -> used.title
    UsedInKind.DIGEST -> runCatching { LocalDate.parse(used.title.take(10)).shortDate() + " Digest" }.getOrDefault(used.title)
}

/**
 * 로그 with server results: one row per source, newest first in day groups, filtered by 생활권 (tree roots), 사람 and 종류. A row
 * tap expands its Threads and where they are used; [onUsedIn] opens that document or Digest line, [onSource] reports that a
 * source text was opened (shown in a sheet, or beside the list when [wide]). Nothing here completes tasks. [initial] sets
 * the filters from outside (a link); the next page follows `next_cursor` when the end is reached.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun SourceLog(
    client: ResultClient,
    onUsedIn: (UsedIn) -> Unit,
    onSource: (String) -> Unit,
    zone: ZoneId,
    modifier: Modifier = Modifier,
    initial: SourceFilters = SourceFilters(),
    onFilters: (SourceFilters) -> Unit = {},
    wide: Boolean? = null,
    reload: Int = 0,
) {
    var filters by remember(initial) { mutableStateOf(initial) }
    fun change(next: SourceFilters) { filters = next; onFilters(next) }
    val tree by produceState<List<TreeNodeView>>(emptyList(), client, reload) { value = client.tree().value }
    var paging by remember(filters, client, reload) { mutableStateOf(SourcePaging()) }
    var loading by remember(filters, client, reload) { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val activeFilters = filters
    val loadMore = {
        if (!loading) {
            loading = true
            val cursor = paging.next
            scope.launch {
                val page = client.sources(cursor, 50, activeFilters.query)
                loading = false
                paging = if (page == null) paging.copy(failed = true)
                else SourcePaging((paging.items + page.items).distinctBy { it.sourceId }, page.nextCursor, loaded = true)
            }
        }
    }
    val load by rememberUpdatedState(loadMore)
    LaunchedEffect(filters, client, reload) { load() }
    val listState = rememberLazyListState()
    val nearEnd by remember {
        derivedStateOf {
            val info = listState.layoutInfo
            info.totalItemsCount > 0 && (info.visibleItemsInfo.lastOrNull()?.index ?: -1) >= info.totalItemsCount - 3
        }
    }
    LaunchedEffect(nearEnd, paging.next, paging.items.size) { if (nearEnd && paging.next != null && paging.loaded) load() }

    // Everyone seen so far, so choosing a person does not shrink the list of people to choose from.
    val known = remember { mutableStateOf(emptySet<String>()) }
    LaunchedEffect(paging.items) { known.value = known.value + paging.items.flatMap { it.people } }
    var expanded by rememberSaveable { mutableStateOf(emptyList<String>()) }
    var picking by remember { mutableStateOf<String?>(null) }
    var viewing by remember { mutableStateOf<SourceLogEntry?>(null) }
    val open: (SourceLogEntry) -> Unit = { viewing = it; onSource(it.sourceId) }

    BoxWithConstraints(modifier.fillMaxSize()) {
        val twoPane = wide ?: isWide(maxWidth, maxHeight)
        TwoPane(
            list = {
                Column(Modifier.fillMaxSize().testTag("source-log")) {
                    Row(
                        Modifier.horizontalScroll(rememberScrollState()).padding(horizontal = 16.dp, vertical = 4.dp),
                        horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically,
                    ) {
                        PhoneChip("전체", filters.sphere == null, { change(filters.copy(sphere = null)) }, Modifier.testTag("log-sphere-all"))
                        tree.filter { it.parent == null }.forEach { root ->
                            PhoneChip(root.label, filters.sphere == root.path, { change(filters.copy(sphere = root.path)) }, Modifier.testTag("log-sphere-${root.path}"))
                        }
                        PhoneChip("사람 ${filters.person ?: ""} ▾".replace("  ", " "), filters.person != null, { picking = "person" }, Modifier.testTag("log-person-chip"))
                        PhoneChip("종류 ${filters.kind ?: ""} ▾".replace("  ", " "), filters.kind != null, { picking = "kind" }, Modifier.testTag("log-kind-chip"))
                    }
                    val rows = remember(paging.items, zone) { sourceRows(paging.items, zone) }
                    Muted(
                        "원본 ${paging.items.size} · Thread ${paging.items.sumOf { it.threads.size }}",
                        Modifier.padding(horizontal = 16.dp, vertical = 4.dp).testTag("log-counts"),
                    )
                    LazyColumn(Modifier.weight(1f).testTag("source-list"), listState, contentPadding = PaddingValues(horizontal = 16.dp, vertical = 4.dp)) {
                        if (paging.failed && paging.items.isEmpty()) item("failed") {
                            Column {
                                Muted("불러오지 못했어요. 연결을 확인해 주세요.", Modifier.testTag("log-unavailable"))
                                TextButton({ load() }, Modifier.defaultMinSize(minHeight = 48.dp).testTag("log-retry")) { Text("다시 시도") }
                            }
                        } else if (!paging.loaded) item("loading") { Muted("불러오는 중", Modifier.testTag("log-loading")) }
                        else if (paging.items.isEmpty()) item("empty") { Muted("조건에 맞는 원본이 없어요", Modifier.testTag("log-empty")) }
                        items(rows, key = { it.key }) { row ->
                            when (row) {
                                is SourceListRow.Day -> Text(
                                    row.title, Modifier.padding(top = 12.dp, bottom = 4.dp),
                                    style = MaterialTheme.typography.labelMedium, color = WeaverTokens.muted,
                                )
                                is SourceListRow.Item -> SourceRow(
                                    row.entry, zone, row.entry.sourceId in expanded,
                                    { expanded = if (row.entry.sourceId in expanded) expanded - row.entry.sourceId else expanded + row.entry.sourceId },
                                    onUsedIn, open,
                                )
                            }
                        }
                        if (paging.failed && paging.items.isNotEmpty()) item("more-failed") {
                            TextButton({ load() }, Modifier.defaultMinSize(minHeight = 48.dp).testTag("log-retry")) { Text("더 불러오지 못했어요 · 다시 시도") }
                        }
                    }
                }
            },
            detail = if (twoPane) viewing?.let { entry ->
                {
                    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        SourceText(client, entry)
                        TextButton({ viewing = null }, Modifier.defaultMinSize(minHeight = 48.dp).testTag("source-text-close")) { Text("닫기") }
                    }
                }
            } else null,
            emptyDetail = "원본의 원문 보기를 누르면 여기에 보여요", wide = twoPane,
        )
        if (!twoPane) viewing?.let { entry ->
            ModalBottomSheet(onDismissRequest = { viewing = null }) {
                Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    SourceText(client, entry)
                    TextButton({ viewing = null }, Modifier.defaultMinSize(minHeight = 48.dp)) { Text("닫기") }
                }
            }
        }
        when (picking) {
            "person" -> PickSheet(
                "사람", (known.value + listOfNotNull(filters.person)).sorted(), filters.person, "log-pick-person", "아직 사람이 없어요",
                { picking = null }, { change(filters.copy(person = it)); picking = null },
            )
            "kind" -> PickSheet("종류", SourceKinds, filters.kind, "log-pick-kind", "", { picking = null }, { change(filters.copy(kind = it)); picking = null })
        }
    }
}

private sealed interface SourceListRow {
    val key: String
    data class Day(val title: String) : SourceListRow { override val key get() = "day-$title" }
    data class Item(val entry: SourceLogEntry) : SourceListRow { override val key get() = entry.sourceId }
}

/** Day headers (`M월 d일 E`) between the rows, in the order the server sent them (newest first). */
private fun sourceRows(entries: List<SourceLogEntry>, zone: ZoneId): List<SourceListRow> {
    val rows = mutableListOf<SourceListRow>()
    var day: String? = null
    for (entry in entries) {
        val title = Instant.parse(entry.occurredAt).atZone(zone).format(dayFormat)
        if (title != day) { rows += SourceListRow.Day(title); day = title }
        rows += SourceListRow.Item(entry)
    }
    return rows
}

@Composable
private fun SourceRow(
    entry: SourceLogEntry, zone: ZoneId, open: Boolean, onToggle: () -> Unit,
    onUsedIn: (UsedIn) -> Unit, onSource: (SourceLogEntry) -> Unit,
) {
    val at = Instant.parse(entry.occurredAt).atZone(zone)
    val where = listOfNotNull("%02d:%02d".format(at.hour, at.minute), entry.paths.firstOrNull()).joinToString(" · ")
    Surface(
        Modifier.fillMaxWidth().padding(vertical = 3.dp).testTag("source-row-${entry.sourceId}"),
        shape = RoundedCornerShape(WeaverTokens.number("radius.card").dp), color = WeaverTokens.surface, border = BorderStroke(1.dp, WeaverTokens.line),
    ) {
        Column {
            Row(
                Modifier.fillMaxWidth().defaultMinSize(minHeight = 48.dp)
                    .clickable(role = Role.Button, onClickLabel = if (open) "접기" else "펼치기", onClick = onToggle)
                    .semantics { stateDescription = if (open) "펼쳐짐" else "접힘" }
                    .padding(horizontal = 12.dp, vertical = 8.dp).testTag("source-toggle-${entry.sourceId}"),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(Modifier.weight(1f)) {
                    Text(entry.label, style = MaterialTheme.typography.bodyLarge, color = WeaverTokens.ink)
                    Muted(where)
                }
                Muted("Thread ${entry.threads.size}", Modifier.padding(start = 8.dp))
            }
            if (open) Column(Modifier.padding(horizontal = 12.dp, vertical = 4.dp).testTag("source-detail-${entry.sourceId}"), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                entry.threads.forEach { t ->
                    Row(Modifier.fillMaxWidth().testTag("source-thread-${t.threadId}"), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Box(Modifier.size(8.dp).background(threadKindStyle(t.kind).color, CircleShape))
                        Text("${t.kind} · ${t.content ?: ""}".trimEnd(' ', '·'), style = MaterialTheme.typography.bodyMedium)
                    }
                }
                Row(Modifier.horizontalScroll(rememberScrollState()), verticalAlignment = Alignment.CenterVertically) {
                    Muted("쓰인 곳:")
                    entry.usedIn.forEachIndexed { i, used ->
                        TextButton({ onUsedIn(used) }, Modifier.defaultMinSize(minHeight = 48.dp).testTag("source-used-${entry.sourceId}-$i")) {
                            Text(usedInLabel(used), color = WeaverTokens.indigo)
                        }
                    }
                    TextButton({ onSource(entry) }, Modifier.defaultMinSize(minHeight = 48.dp).testTag("source-text-${entry.sourceId}")) {
                        Text("원문 보기", color = WeaverTokens.indigo)
                    }
                }
            }
        }
    }
}

/** The source's whole text, read through the evidence of a sentence that cites it (the sources API carries no text). */
@Composable
private fun SourceText(client: ResultClient, entry: SourceLogEntry) {
    val cite = entry.usedIn.firstOrNull { it.kind == UsedInKind.DOCUMENT && it.bulletId != null }
        ?: entry.usedIn.firstOrNull { it.bulletId != null || it.blockId != null }
    val load by produceState<EvidenceLoad>(EvidenceLoad.Loading, client, entry.sourceId) {
        value = if (cite == null) EvidenceLoad.Unavailable
        else client.evidence(cite.documentId, checkNotNull(cite.bulletId ?: cite.blockId))?.let { EvidenceLoad.Ready(it) } ?: EvidenceLoad.Unavailable
    }
    Column(Modifier.testTag("source-text-pane"), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(entry.label, style = MaterialTheme.typography.titleLarge, color = WeaverTokens.indigo)
        when (val state = load) {
            EvidenceLoad.Loading -> Muted("원문을 불러오는 중이에요", Modifier.testTag("source-text-loading"))
            EvidenceLoad.Unavailable -> Muted("지금은 원문을 불러올 수 없어요. 연결을 확인해 주세요.", Modifier.testTag("source-text-unavailable"))
            is EvidenceLoad.Ready -> state.evidence.sources.filter { it.sourceId == entry.sourceId }.let { found ->
                if (found.isEmpty()) Muted("원문을 찾지 못했어요", Modifier.testTag("source-text-unavailable")) else found.forEach { EvidenceSourceBlock(it) }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun PickSheet(
    title: String, options: List<String>, selected: String?, tag: String, empty: String,
    dismiss: () -> Unit, onPick: (String?) -> Unit,
) {
    ModalBottomSheet(onDismissRequest = dismiss) {
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(bottom = 16.dp).testTag("$tag-sheet")) {
            Text(title, Modifier.padding(horizontal = 16.dp, vertical = 8.dp), style = MaterialTheme.typography.titleLarge, color = WeaverTokens.indigo)
            PickRow("전체 $title", selected == null, Modifier.testTag("$tag-all")) { onPick(null) }
            if (options.isEmpty() && empty.isNotEmpty()) Muted(empty, Modifier.padding(horizontal = 16.dp))
            options.forEach { option -> PickRow(option, option == selected, Modifier.testTag("$tag-$option")) { onPick(option) } }
        }
    }
}

@Composable
private fun PickRow(label: String, on: Boolean, modifier: Modifier, onClick: () -> Unit) {
    Row(
        modifier.fillMaxWidth().defaultMinSize(minHeight = 48.dp)
            .selectable(on, role = Role.RadioButton, onClick = onClick)
            .padding(horizontal = 16.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(label, Modifier.weight(1f), style = MaterialTheme.typography.bodyLarge, color = if (on) WeaverTokens.indigo else WeaverTokens.ink)
        if (on) Text("✓", color = WeaverTokens.indigo)
    }
}
