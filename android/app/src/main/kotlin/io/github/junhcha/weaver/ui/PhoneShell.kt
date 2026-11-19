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
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
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
    tagHost: TagEditHost? = null,
    initialSubject: String? = null,
    onSubjectBack: (() -> Unit)? = null,
) {
    // Every resume re-reads the snapshot and recomputes "now" so day groups never go stale.
    var resumes by rememberSaveable { mutableIntStateOf(0) }
    val first = remember { booleanArrayOf(true) }
    LifecycleResumeEffect(Unit) {
        if (first[0]) first[0] = false else resumes++
        onPauseOrDispose { }
    }
    var edits by rememberSaveable { mutableIntStateOf(0) }
    var offlineSince by remember { mutableStateOf<Instant?>(null) }
    var readVersion by remember { mutableIntStateOf(0) }
    val snapshot by produceState<PhoneSnapshot?>(null, readModel, resumes, edits) {
        value = readModel.snapshot()
        offlineSince = readModel.offlineSince
        readVersion++
    }
    val onEdited: () -> Unit = { edits++ }
    val current = remember(resumes, snapshot, readVersion, edits) { now() }
    var tab by rememberSaveable { mutableStateOf(initialTab) }
    io.github.junhcha.weaver.diagnostics.CrashDiagnostics.screen =
        io.github.junhcha.weaver.diagnostics.CrashDiagnostics.screen.copy(last_tab = tab.name)

    var person by rememberSaveable { mutableStateOf(initialSubject.takeIf { initialTab == PhoneTab.PEOPLE }) }
    var area by rememberSaveable { mutableStateOf(initialSubject.takeIf { initialTab == PhoneTab.AREAS }) }
    var page by rememberSaveable { mutableStateOf<String?>(null) }
    @Suppress("UNUSED_VARIABLE") val observedReadVersion = readVersion
    val nowUi = readModel.nowController?.uiState?.collectAsState()?.value
    val controller = readModel.nowController?.takeIf { nowUi?.supported == true && nowUi.snapshot != null }
    var selectedThread by rememberSaveable { mutableStateOf<String?>(null) }
    val currentEpoch = nowUi?.snapshot?.epoch
    var detailEpoch by remember { mutableStateOf(currentEpoch) }
    LaunchedEffect(currentEpoch) {
        if (detailEpoch != currentEpoch) selectedThread = null
        detailEpoch = currentEpoch
    }
    LaunchedEffect(snapshot, nowUi?.nextRefreshAt, readModel, readVersion, resumes) {
        val moment = now()
        val next = nowUi?.nextRefreshAt ?: moment.atZone(io.github.junhcha.weaver.phone.SeoulZone)
            .toLocalDate().plusDays(1).atStartOfDay(io.github.junhcha.weaver.phone.SeoulZone).toInstant()
        kotlinx.coroutines.delay(java.time.Duration.between(moment, next).toMillis().coerceAtLeast(1000))
        resumes++
    }
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
    val closePerson: () -> Unit = { if (onSubjectBack != null) onSubjectBack() else person = null }
    val closeArea: () -> Unit = { if (onSubjectBack != null) onSubjectBack() else area = null }
    when {
        tab == PhoneTab.PEOPLE && person != null -> BackHandler(onBack = closePerson)
        tab == PhoneTab.AREAS && area != null -> BackHandler(onBack = closeArea)
        tab == PhoneTab.WIKI && page != null -> BackHandler { page = null }
        tab != PhoneTab.NOW -> BackHandler { tab = PhoneTab.NOW }
    }
    selectedThread?.let { id ->
        val target = (nowUi?.snapshot?.threads.orEmpty() + data?.threads.orEmpty() + nowUi?.suppressed.orEmpty()).firstOrNull { it.id == id }
        if (target != null) NowThreadSheet(target,
            data?.threads.orEmpty().filter { it.historyTargetForDetail(id, data?.threads.orEmpty()) }, controller,
            { edits++ }, { selectedThread = null })
    }
    Column(Modifier.fillMaxSize()) {
        if (readModel.isExample) ExampleBadge()
        if (nowUi?.supported == true && controller == null) Muted(if (nowUi?.problem == "response_too_large") "지금 목록이 너무 커요. 서버 페이지네이션 지원이 필요해요. 자동 재시도를 멈췄어요." else "지금 목록을 확인하지 못했어요. 연결 후 다시 불러와 주세요.", Modifier.padding(16.dp))
        offlineSince?.let { OfflineBanner(it, nowUi?.snapshot?.zone ?: io.github.junhcha.weaver.phone.SeoulZone) }
        Box(Modifier.weight(1f)) {
            when (tab) {
                PhoneTab.NOW -> NowScreen(
                    if (nowUi?.supported == true) nowUi?.visible else data?.let { nowView(it, current) }, counts, if (nowUi?.online == true) nowUi.snapshot!!.generatedAt else current, connected = !noData, onOpenMap, onOpenSettings,
                    zone = nowUi?.snapshot?.zone ?: io.github.junhcha.weaver.phone.SeoulZone,
                    onThread = { selectedThread = it.id },
                    handling = controller?.let { host -> { NowHandlingBar(host, { edits++ }, { selectedThread = it.id }, data?.threads.orEmpty()) } },
                )
                PhoneTab.PEOPLE -> if (data == null) Loading() else person?.let { name ->
                    TimelineScreen(data, name, closePerson, tagHost, onEdited)
                } ?: SubjectList("사람", peopleEntries(data), emptyText ?: "아직 없어요") { person = it }
                PhoneTab.AREAS -> if (data == null) Loading() else area?.let { name ->
                    TimelineScreen(data, name, closeArea, tagHost, onEdited)
                } ?: SubjectList("영역", areaEntries(data), emptyText ?: "아직 없어요") { area = it }
                PhoneTab.WIKI -> if (data == null) Loading() else {
                    val open = page?.let { id -> data.wikiPages.firstOrNull { it.id == id } }
                    if (open != null) {
                        // The list carries no bodies: fetch (or read the cached) body when the page is opened.
                        val body by produceState<String?>(open.body.takeIf { it.isNotEmpty() }, open.id, open.sha256) {
                            value = readModel.wikiBody(open) ?: value
                        }
                        WikiViewer(data, open.copy(body = body.orEmpty()), { page = null }, ::open, loading = body == null)
                    }
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

/** Shown while the screens use the last copy from the server because it cannot be reached right now. */
@Composable
private fun OfflineBanner(since: Instant, zone: java.time.ZoneId) {
    val at = since.atZone(zone)
    Box(
        Modifier.fillMaxWidth().background(WeaverTokens.line).padding(vertical = 4.dp).testTag("offline-banner"),
        contentAlignment = Alignment.Center,
    ) {
        Text("오프라인 · ${at.monthValue}/${at.dayOfMonth} ${"%02d:%02d".format(at.hour, at.minute)} 기준 사본",
            style = MaterialTheme.typography.labelMedium, color = WeaverTokens.ink)
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

private fun io.github.junhcha.weaver.phone.PhoneThread.historyTargetForDetail(id: String, threads: List<io.github.junhcha.weaver.phone.PhoneThread>): Boolean {
    var row = this
    val seen = mutableSetOf<String>()
    while (seen.add(row.id)) {
        val target = row.correction?.target ?: row.retracts ?: row.executes ?: row.resolves ?: row.amends ?: row.updates ?: return false
        if (target == id) return true
        row = threads.firstOrNull { it.id == target } ?: return false
    }
    return false
}
