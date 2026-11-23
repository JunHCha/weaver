package io.github.junhcha.weaver.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.platform.LocalContext
import io.github.junhcha.weaver.phone.CompleteResult
import io.github.junhcha.weaver.phone.DoneStore
import io.github.junhcha.weaver.schema.TaskItem
import kotlinx.coroutines.launch
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
import io.github.junhcha.weaver.phone.resolveLink
import java.time.Instant

enum class PhoneTab(val label: String) { NOW("지금"), AREAS("영역"), LOG("로그") }

private enum class LogFilter(val label: String) { ALL("전체"), PEOPLE("사람") }

/**
 * Three-tab phone shell over a read-only [PhoneReadModel]. [onOpenSettings] leaves to the settings screen and
 * [onReview] to the review screen; both stay outside this shell. [counterparts] maps package id to counterpart name
 * for the approval rows. In a wide layout [selectedReview] is shown inline beside the 지금 list; [onCloseReview] and
 * [onReviewSubmitted] report its close and submission to whoever owns the selection.
 */
@Composable
fun PhoneShell(
    readModel: PhoneReadModel,
    counts: StatusCounts,
    now: () -> Instant,
    onOpenSettings: () -> Unit,
    onReview: (String) -> Unit,
    initialTab: PhoneTab = PhoneTab.NOW,
    /** Opens this result document in 영역 first (a deep link); only used when the server has `results`. */
    initialDocument: LinkTarget.Document? = null,
    /** With [initialDocument]: open its 피드백 tab (an overwrite alert); only used when the server has `feedback`. */
    initialFeedback: Boolean = false,
    tagHost: TagEditHost? = null,
    counterparts: Map<String, String> = emptyMap(),
    selectedReview: String? = null,
    onCloseReview: () -> Unit = {},
    onReviewSubmitted: () -> Unit = {},
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
    var tab by rememberSaveable { mutableStateOf(if (initialDocument != null) PhoneTab.AREAS else initialTab) }
    io.github.junhcha.weaver.diagnostics.CrashDiagnostics.screen =
        io.github.junhcha.weaver.diagnostics.CrashDiagnostics.screen.copy(last_tab = tab.crashName)

    var logFilter by rememberSaveable { mutableStateOf(LogFilter.ALL) }
    var person by rememberSaveable { mutableStateOf<String?>(null) }
    // 로그 with server results: the filters of the source list (a link can set them).
    var logSphere by rememberSaveable { mutableStateOf<String?>(null) }
    var logPerson by rememberSaveable { mutableStateOf<String?>(null) }
    var logKind by rememberSaveable { mutableStateOf<String?>(null) }
    var area by rememberSaveable { mutableStateOf<String?>(null) }
    var page by rememberSaveable { mutableStateOf<String?>(null) }
    // 영역 tab with server results: the chosen tree node, its open document, the sentence whose evidence is shown and the
    // bullet a link asked to scroll to.
    var areaPath by rememberSaveable { mutableStateOf<String?>(null) }
    var documentId by rememberSaveable { mutableStateOf(initialDocument?.id) }
    var evidenceBullet by rememberSaveable { mutableStateOf<String?>(null) }
    var documentFocus by rememberSaveable { mutableStateOf(initialDocument?.bulletId) }
    var documentFeedback by rememberSaveable { mutableStateOf(initialDocument != null && initialFeedback) }
    @Suppress("UNUSED_VARIABLE") val observedReadVersion = readVersion
    // 지금 from server results: the documents a card or task opens beside the list (wide), and the tasks checked on this phone.
    var nowDocumentId by rememberSaveable { mutableStateOf<String?>(null) }
    var nowDocumentFocus by rememberSaveable { mutableStateOf<String?>(null) }
    var returnToNow by rememberSaveable { mutableStateOf(false) }
    val results = readModel.results
    val resultsOn by produceState<Boolean?>(if (results == null) false else null, results, resumes) {
        value = results?.enabled() ?: false
    }
    val appContext = LocalContext.current.applicationContext
    val done = readModel.doneStore ?: remember { DoneStore(java.io.File(appContext.cacheDir, "done-local.json")) }
    val doneIds by done.ids.collectAsState()
    val scope = rememberCoroutineScope()
    var completeError by remember { mutableStateOf<String?>(null) }
    var taskChecks by remember { mutableIntStateOf(0) }
    var undoable by remember { mutableStateOf<Pair<TaskItem, io.github.junhcha.weaver.phone.PendingRequest>?>(null) }
    LaunchedEffect(undoable) { if (undoable != null) { kotlinx.coroutines.delay(5_000); undoable = null } }
    /** One path for every checked task (the 지금 list and a document line): hide, complete, show the error when refused. */
    fun checkTask(task: TaskItem) {
        scope.launch {
            val controller = readModel.nowController
            val result = completeTask(task, done, System.currentTimeMillis()) { id, revision ->
                controller?.complete(id, revision) ?: CompleteResult.Rejected("edit_unavailable")
            }
            completeError = completeProblem(result)
            undoable = (result as? CompleteResult.Done)?.let { task to it.operation }
            taskChecks++
        }
    }
    val nowUi = readModel.nowController?.uiState?.collectAsState()?.value
    val controller = readModel.nowController?.takeIf { nowUi?.supported == true && nowUi.snapshot != null }
    // A queued check the server refused later (or that failed) must not stay hidden for good.
    LaunchedEffect(nowUi?.operations) { nowUi?.operations?.let { done.reconcile(it); taskChecks++ } }
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

    val logFiltered = logSphere != null || logPerson != null || logKind != null
    fun showLog(filters: SourceFilters) {
        tab = PhoneTab.LOG; page = null
        logSphere = filters.sphere; logPerson = filters.person; logKind = filters.kind
    }
    /** "로그에서 시간순으로 보기": the 로그 tab filtered by the subject's 생활권 or person (the old 사람 screen without results). */
    fun openLogFor(subject: String) {
        if (resultsOn == true && results != null) scope.launch { showLog(logFiltersFor(subject, results.tree().value)) }
        else { tab = PhoneTab.LOG; logFilter = LogFilter.PEOPLE; person = subject }
    }
    fun open(target: LinkTarget) {
        when (target) {
            is LinkTarget.Person ->
                if (resultsOn == true && results != null) showLog(SourceFilters(person = target.name))
                else { tab = PhoneTab.LOG; logFilter = LogFilter.PEOPLE; page = null; person = target.name }
            is LinkTarget.Area ->
                if (resultsOn == true && results != null) {
                    documentFeedback = false
                    // The matching tree node (and its document) once the tree is read; the plain tree when none matches.
                    tab = PhoneTab.AREAS; page = null; areaPath = null; documentId = null; documentFocus = null; evidenceBullet = null
                    scope.launch {
                        val node = areaNodeFor(target.name, results.tree().value)
                        areaPath = node?.path; documentId = node?.documentId
                    }
                } else { tab = PhoneTab.AREAS; page = null; area = target.name }
            is LinkTarget.Page -> { documentFeedback = false; tab = PhoneTab.AREAS; area = null; page = target.id; documentId = null; areaPath = null; evidenceBullet = null }
            is LinkTarget.Document -> {
                documentFeedback = false
                tab = PhoneTab.AREAS; area = null; page = null; areaPath = null
                documentId = target.id; documentFocus = target.bulletId; evidenceBullet = null
            }
            LinkTarget.None -> Unit
        }
    }
    BoxWithConstraints(Modifier.fillMaxSize()) {
        val wide = isWide(maxWidth, maxHeight)
        when {
            wide && tab == PhoneTab.NOW && selectedThread != null -> BackHandler { selectedThread = null }
            wide && tab == PhoneTab.NOW && nowDocumentId != null -> BackHandler {
                if (evidenceBullet != null) evidenceBullet = null else { nowDocumentId = null; nowDocumentFocus = null }
            }
            tab == PhoneTab.LOG && resultsOn == true && results != null && logFiltered ->
                BackHandler { logSphere = null; logPerson = null; logKind = null }
            tab == PhoneTab.LOG && logFilter == LogFilter.PEOPLE && person != null -> BackHandler { person = null }
            tab == PhoneTab.AREAS && (documentId != null || areaPath != null) -> BackHandler {
                when {
                    evidenceBullet != null -> evidenceBullet = null
                    documentId != null -> { documentId = null; documentFocus = null; if (returnToNow) { returnToNow = false; tab = PhoneTab.NOW } }
                    else -> areaPath = null
                }
            }
            tab == PhoneTab.AREAS && area != null -> BackHandler { area = null }
            tab == PhoneTab.AREAS && page != null -> BackHandler { page = null }
            tab != PhoneTab.NOW -> BackHandler { tab = PhoneTab.NOW }
        }
        val threadTarget = selectedThread?.let { id ->
            (nowUi?.snapshot?.threads.orEmpty() + data?.threads.orEmpty() + nowUi?.suppressed.orEmpty()).firstOrNull { it.id == id }
        }
        val threadHistory = threadTarget?.let { t -> data?.threads.orEmpty().filter { it.historyTargetForDetail(t.id, data?.threads.orEmpty()) } }.orEmpty()
        /** A card or task of 지금: beside the list when wide, otherwise the document fills 영역 and back returns here. */
        fun openFromNow(target: LinkTarget.Document?) {
            if (target == null) return
            selectedThread = null
            if (wide) { onCloseReview(); nowDocumentId = target.id; nowDocumentFocus = target.bulletId; evidenceBullet = null }
            else { open(target); returnToNow = true }
        }
        // Wide 지금 shows the thread inline; every other place (and narrow) keeps the bottom sheet.
        if (threadTarget != null && !(wide && tab == PhoneTab.NOW))
            NowThreadSheet(threadTarget, threadHistory, controller, { edits++ }, { selectedThread = null })
        Column(Modifier.fillMaxSize()) {
            if (readModel.isExample) ExampleBadge()
            if (nowUi?.supported == true && controller == null) Muted(if (nowUi?.problem == "response_too_large") "지금 목록이 너무 커요. 서버 페이지네이션 지원이 필요해요. 자동 재시도를 멈췄어요." else "지금 목록을 확인하지 못했어요. 연결 후 다시 불러와 주세요.", Modifier.padding(16.dp))
            undoable?.let { (task, operation) ->
                CompleteToast(task.content) {
                    undoable = null
                    scope.launch {
                        completeError = undoCompletion(task, operation, done) { readModel.nowController?.undo(it) ?: error("edit_unavailable") }
                        taskChecks++
                    }
                }
            }
            completeError?.let { Muted(it, Modifier.padding(horizontal = 16.dp, vertical = 4.dp).testTag("complete-error")) }
            offlineSince?.let { OfflineBanner(it, nowUi?.snapshot?.zone ?: io.github.junhcha.weaver.phone.SeoulZone) }
            Box(Modifier.weight(1f)) {
                when (tab) {
                    PhoneTab.NOW -> TwoPane(
                        list = {
                            NowScreen(
                                if (resultsOn == null) null else if (nowUi?.supported == true) nowUi?.visible else data?.let { nowView(it, current) }, counts, if (nowUi?.online == true) nowUi.snapshot!!.generatedAt else current, connected = !noData, onOpenSettings,
                                zone = nowUi?.snapshot?.zone ?: io.github.junhcha.weaver.phone.SeoulZone,
                                onThread = { selectedThread = it.id; nowDocumentId = null; if (wide) onCloseReview() },
                                onReview = { selectedThread = null; nowDocumentId = null; onReview(it) }, counterparts = counterparts,
                                results = if (resultsOn == true && results != null) ({
                                    NowResultsContent(results, done, current, nowUi?.snapshot?.zone ?: io.github.junhcha.weaver.phone.SeoulZone,
                                        resumes + taskChecks, onCheck = ::checkTask, onCard = { openFromNow(it.openTarget()) }, onTask = { openFromNow(it.openTarget()) })
                                }) else null,
                                handling = controller?.let { host -> { NowHandlingBar(host, { edits++ }, { selectedThread = it.id; if (wide) onCloseReview() }, data?.threads.orEmpty()) } },
                            )
                        },
                        detail = when {
                            !wide -> null
                            selectedReview != null -> ({ ReviewPane(selectedReview, onCloseReview, onReviewSubmitted) })
                            threadTarget != null -> ({
                                Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                                    Text("Thread 상세", style = MaterialTheme.typography.titleLarge)
                                    NowThreadDetail(threadTarget, threadHistory, controller, { edits++ }, { selectedThread = null })
                                    TextButton(onClick = { selectedThread = null }, Modifier.defaultMinSize(minHeight = 48.dp)) { Text("닫기") }
                                }
                            })
                            nowDocumentId != null && results != null -> ({
                                DocumentRoute(results, checkNotNull(nowDocumentId), null, evidenceBullet, { evidenceBullet = it }, ::checkTask,
                                    ::openLogFor, nowDocumentFocus, false,
                                    { nowDocumentId = null; nowDocumentFocus = null; evidenceBullet = null }, resumes + taskChecks, doneIds)
                            })
                            else -> null
                        },
                        emptyDetail = "승인 항목이나 일정을 선택하세요", wide = wide,
                    )
                    PhoneTab.AREAS -> if (data == null) Loading() else {
                        if (resultsOn == null) Loading()
                        else if (resultsOn == true && results != null) {
                            val documents by produceState(emptyList<io.github.junhcha.weaver.schema.DocumentSummary>(), results, resumes) {
                                value = results.documents().value
                            }
                            val wiki = page?.let { id -> data.wikiPages.firstOrNull { it.id == id } }
                            if (wiki != null) {
                                // A Wiki page opened by a link: its links resolve to result documents first.
                                val body by produceState<String?>(wiki.body.takeIf { it.isNotEmpty() }, wiki.id, wiki.sha256) {
                                    value = readModel.wikiBody(wiki) ?: value
                                }
                                WikiViewer(data, wiki.copy(body = body.orEmpty()), { page = null }, ::open, loading = body == null,
                                    resolve = { resolveLink(data, it, documents) })
                            } else AreasTab(
                            results, wide, areaPath, documentId, evidenceBullet, documentFocus, resumes,
                            // Choosing a node resets the document first; the node's own document is opened right after by onDocument.
                            onPath = { areaPath = it; documentFeedback = false; documentId = null; documentFocus = null; evidenceBullet = null; returnToNow = false },
                            onDocument = { id, path -> documentFeedback = false; documentId = id; areaPath = path ?: areaPath; documentFocus = null; evidenceBullet = null; returnToNow = false },
                            onBullet = { evidenceBullet = it },
                            onTask = ::checkTask,
                            onLog = ::openLogFor,
                            onCloseDocument = { documentFeedback = false; documentId = null; documentFocus = null; evidenceBullet = null; if (returnToNow) { returnToNow = false; tab = PhoneTab.NOW } },
                            doneIds = doneIds,
                            feedbackFirst = documentFeedback,
                            )
                        }
                        else {
                            val open = page?.let { id -> data.wikiPages.firstOrNull { it.id == id } }
                            TwoPane(
                                list = {
                                    if (noData) SubjectList("영역", emptyList(), checkNotNull(emptyText)) {}
                                    else Column(Modifier.fillMaxSize()) {
                                        SubjectList("영역", areaEntries(data), "아직 없어요", Modifier.weight(1f)) { area = it; page = null }
                                        WikiList(data.wikiPages, "아직 문서가 없어요", Modifier.weight(1f), "문서") { page = it; area = null }
                                    }
                                },
                                detail = when {
                                    area != null -> ({ TimelineScreen(data, checkNotNull(area), { area = null }, tagHost, onEdited) })
                                    open != null -> ({
                                        // The list carries no bodies: fetch (or read the cached) body when the page is opened.
                                        val body by produceState<String?>(open.body.takeIf { it.isNotEmpty() }, open.id, open.sha256) {
                                            value = readModel.wikiBody(open) ?: value
                                        }
                                        WikiViewer(data, open.copy(body = body.orEmpty()), { page = null }, ::open, loading = body == null)
                                    })
                                    else -> null
                                },
                                emptyDetail = "영역이나 문서를 선택하세요", wide = wide,
                            )
                        }
                    }
                    PhoneTab.LOG -> if (resultsOn == null) Loading()
                    else if (resultsOn == true && results != null) SourceLog(
                        results, onUsedIn = { open(LinkTarget.Document(it.documentId, it.bulletId ?: it.blockId)) }, onSource = {},
                        zone = nowUi?.snapshot?.zone ?: io.github.junhcha.weaver.phone.SeoulZone,
                        modifier = Modifier.testTag("log-screen"), initial = SourceFilters(logSphere, logPerson, logKind),
                        onFilters = { logSphere = it.sphere; logPerson = it.person; logKind = it.kind },
                        wide = wide, reload = resumes,
                    )
                    else if (data == null) Loading() else Column(Modifier.fillMaxSize().testTag("log-screen")) {
                        val timeline = logFilter == LogFilter.PEOPLE && person != null
                        if (!timeline) Row(Modifier.padding(horizontal = 16.dp, vertical = 4.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            LogFilter.entries.forEach { f ->
                                PhoneChip(f.label, logFilter == f, { logFilter = f }, Modifier.testTag("log-filter-${f.name}"))
                            }
                        }
                        when {
                            timeline -> TimelineScreen(data, checkNotNull(person), { person = null }, tagHost, onEdited)
                            logFilter == LogFilter.PEOPLE -> SubjectList("사람", peopleEntries(data), emptyText ?: "아직 없어요", Modifier.weight(1f)) { person = it }
                            else -> LogThreads(data.threads, emptyText ?: "아직 기록이 없어요", nowUi?.snapshot?.zone ?: io.github.junhcha.weaver.phone.SeoulZone, { selectedThread = it.id }, Modifier.weight(1f))
                        }
                    }
                }
            }
            BottomTabs(tab) { selected ->
                // Tapping the current tab returns to its list.
                returnToNow = false
                completeError = null
                if (selected == tab) when (selected) {
                    PhoneTab.AREAS -> { area = null; page = null; areaPath = null; documentId = null; documentFocus = null; evidenceBullet = null }
                    PhoneTab.LOG -> { person = null; logSphere = null; logPerson = null; logKind = null }
                    PhoneTab.NOW -> Unit
                }
                tab = selected
            }
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

/** Crash reports use the server's tab names; PEOPLE and WIKI are only still accepted for older APKs. */
private val PhoneTab.crashName: String get() = when (this) {
    PhoneTab.NOW -> "NOW"
    PhoneTab.AREAS -> "AREAS"
    PhoneTab.LOG -> "LOG"
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
