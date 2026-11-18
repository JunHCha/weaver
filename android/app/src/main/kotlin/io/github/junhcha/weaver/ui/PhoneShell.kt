package io.github.junhcha.weaver.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import io.github.junhcha.weaver.phone.isEmpty
import io.github.junhcha.weaver.pipeline.MapItem
import io.github.junhcha.weaver.phone.LinkTarget
import io.github.junhcha.weaver.phone.PhoneReadModel
import io.github.junhcha.weaver.phone.PhoneSnapshot
import io.github.junhcha.weaver.phone.StatusCounts
import io.github.junhcha.weaver.phone.nowView
import java.time.Instant

enum class PhoneTab(val label: String) { NOW("지금"), PEOPLE("사람"), AREAS("영역"), WIKI("Wiki") }

/**
 * Four-tab phone shell over a read-only [PhoneReadModel]. [onOpenMap] and [onOpenSettings] leave to the existing
 * pipeline map and settings screens, which stay outside this shell.
 */
@Composable
fun PhoneShell(
    readModel: PhoneReadModel,
    counts: StatusCounts,
    now: () -> Instant,
    onOpenMap: () -> Unit,
    onOpenSettings: () -> Unit,
    initialTab: PhoneTab = PhoneTab.NOW,
) {
    // Every resume re-reads the snapshot and recomputes "now" so day groups never go stale.
    var resumes by rememberSaveable { mutableIntStateOf(0) }
    val first = remember { booleanArrayOf(true) }
    LifecycleResumeEffect(Unit) {
        if (first[0]) first[0] = false else resumes++
        onPauseOrDispose { }
    }
    val snapshot by produceState<PhoneSnapshot?>(null, readModel, resumes) { value = readModel.snapshot() }
    val current = remember(resumes, snapshot) { now() }
    var tab by rememberSaveable { mutableStateOf(initialTab) }
    var person by rememberSaveable { mutableStateOf<String?>(null) }
    var area by rememberSaveable { mutableStateOf<String?>(null) }
    var page by rememberSaveable { mutableStateOf<String?>(null) }
    val data = snapshot
    val noData = data?.isEmpty == true
    val emptyText = if (noData) "연결된 데이터 없음" else null

    fun open(target: LinkTarget) {
        when (target) {
            is LinkTarget.Person -> { tab = PhoneTab.PEOPLE; person = target.name }
            is LinkTarget.Area -> { tab = PhoneTab.AREAS; area = target.name }
            is LinkTarget.Page -> { tab = PhoneTab.WIKI; page = target.id }
            LinkTarget.None -> Unit
        }
    }
    when {
        tab == PhoneTab.PEOPLE && person != null -> BackHandler { person = null }
        tab == PhoneTab.AREAS && area != null -> BackHandler { area = null }
        tab == PhoneTab.WIKI && page != null -> BackHandler { page = null }
        tab != PhoneTab.NOW -> BackHandler { tab = PhoneTab.NOW }
    }
    Column(Modifier.fillMaxSize()) {
        if (readModel.isExample) ExampleBadge()
        Box(Modifier.weight(1f)) {
            when (tab) {
                PhoneTab.NOW -> NowScreen(
                    data?.let { nowView(it, current) }, counts, current, connected = !noData, onOpenMap, onOpenSettings,
                )
                PhoneTab.PEOPLE -> if (data == null) Loading() else person?.let { name ->
                    TimelineScreen(data, name) { person = null }
                } ?: SubjectList("사람", peopleEntries(data), emptyText ?: "아직 없어요") { person = it }
                PhoneTab.AREAS -> if (data == null) Loading() else area?.let { name ->
                    TimelineScreen(data, name) { area = null }
                } ?: SubjectList("영역", areaEntries(data), emptyText ?: "아직 없어요") { area = it }
                PhoneTab.WIKI -> if (data == null) Loading() else {
                    val open = page?.let { id -> data.wikiPages.firstOrNull { it.id == id } }
                    if (open != null) WikiViewer(data, open, { page = null }, ::open)
                    else WikiList(data.wikiPages, emptyText ?: "아직 페이지가 없어요") { page = it }
                }
            }
        }
        BottomTabs(tab) { selected ->
            // Tapping the current tab returns to its list.
            if (selected == tab) when (selected) {
                PhoneTab.PEOPLE -> person = null
                PhoneTab.AREAS -> area = null
                PhoneTab.WIKI -> page = null
                PhoneTab.NOW -> Unit
            }
            tab = selected
        }
    }
}

/** Always visible while the data is made up, so example rows are never mistaken for real work. */
@Composable
private fun ExampleBadge() {
    Box(
        Modifier.fillMaxWidth().background(WeaverTokens.indigo).padding(vertical = 4.dp).testTag("example-badge"),
        contentAlignment = Alignment.Center,
    ) { Text("예시 데이터", style = MaterialTheme.typography.labelMedium, color = WeaverTokens.surface) }
}

/** Status strip counts, from the same `MapItem` states the map uses. */
fun List<MapItem>.statusCounts() = StatusCounts(count { it.needsReview }, count { it.processing }, count { it.stopped })

@Composable
private fun Loading() = Muted("불러오는 중", Modifier.padding(16.dp))

@Composable
private fun BottomTabs(current: PhoneTab, onSelect: (PhoneTab) -> Unit) {
    Row(
        Modifier.fillMaxWidth().height(52.dp).background(WeaverTokens.surface)
            .border(1.dp, WeaverTokens.line, androidx.compose.ui.graphics.RectangleShape).testTag("bottom-tabs"),
    ) {
        PhoneTab.entries.forEach { tab ->
            val on = tab == current
            Box(
                Modifier.weight(1f).fillMaxSize()
                    .clickable(role = Role.Tab) { onSelect(tab) }
                    .semantics { role = Role.Tab; selected = on }.testTag("tab-${tab.name}"),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    tab.label, style = if (on) MaterialTheme.typography.labelLarge else MaterialTheme.typography.labelMedium,
                    color = if (on) WeaverTokens.indigo else WeaverTokens.muted,
                )
            }
        }
    }
}
