package io.github.junhcha.weaver.ui

import android.content.Intent
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import io.github.junhcha.weaver.pipeline.*
import io.github.junhcha.weaver.schema.*
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.ZoneId
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

internal fun todayPage(
    page: PipelinePage,
    today: LocalDate = LocalDate.now(),
    zone: ZoneId = ZoneId.systemDefault(),
): Boolean = OffsetDateTime.parse(page.updatedAt).atZoneSameInstant(zone).toLocalDate() == today

internal fun todayDigest(page: PipelinePage, today: LocalDate = LocalDate.now()): Boolean =
    page.path.substringAfterLast('/') == "$today.md"

internal fun todayResults(
    results: List<PipelineItem>,
    today: LocalDate = LocalDate.now(),
): List<PipelinePage> =
    (results.flatMap { it.pages }.filter { todayPage(it, today) } +
            results.mapNotNull { it.digest }.filter { todayDigest(it, today) })
        .distinctBy { it.path to it.sha256 }

internal fun panelResultPages(results: List<PipelineItem>, today: LocalDate = LocalDate.now()): List<PipelinePage> =
    (todayResults(results, today) + results.filter { it.wikiEdit != null }
        .flatMap { it.pages + listOfNotNull(it.digest) }).distinctBy { it.path to it.sha256 }

@Composable
internal fun NodePanel(
    node: MapNode,
    items: List<MapItem>,
    onClose: () -> Unit,
    modifier: Modifier = Modifier,
    focusedItemId: String? = null,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var vault by remember { mutableStateOf(VaultAccess.name(context)) }
    var folderSelected by remember { mutableStateOf(VaultAccess.tree(context) != null) }
    var availability by remember { mutableStateOf(emptySet<String>()) }
    var message by remember { mutableStateOf<String?>(null) }
    var refresh by remember { mutableIntStateOf(0) }
    var approval by rememberSaveable(node) { mutableStateOf<String?>(null) }
    val today = LocalDate.now()
    val results = items.filter { it.node == MapNode.WIKI }
        .sortedBy { if (it.id == focusedItemId) 0 else 1 }.mapNotNull { it.server }
    val pages = panelResultPages(results, today)
    LaunchedEffect(vault) {
        delay(300)
        withContext(Dispatchers.IO) { runCatching { VaultAccess.setName(context, vault) } }
    }
    LifecycleResumeEffect(Unit) {
        refresh++
        onPauseOrDispose {}
    }
    LaunchedEffect(pages, vault, refresh, folderSelected) {
        if (node != MapNode.WIKI) return@LaunchedEffect
        do {
            availability =
                withContext(Dispatchers.IO) {
                    pages
                        .filter { VaultAccess.arrived(context, it) }
                        .map { it.path + ":" + it.sha256 }
                        .toSet()
                }
            delay(10_000)
        } while (isActive)
    }
    val launcher =
        rememberLauncherForActivityResult(ActivityResultContracts.StartActivityForResult()) { result
            ->
            result.data?.data?.let { uri ->
                scope.launch {
                    val selected =
                        withContext(Dispatchers.IO) {
                            runCatching { VaultAccess.select(context, uri) }
                        }
                    selected
                        .onSuccess {
                            folderSelected = true
                            refresh++
                            message = "폰의 vault 폴더를 연결했어요"
                        }
                        .onFailure { message = it.message ?: "vault 폴더를 읽지 못했어요" }
                }
            }
        }
    Column(
        modifier.padding(20.dp).verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(node.label, style = MaterialTheme.typography.headlineSmall)
            TextButton(onClick = onClose) { Text("닫기") }
        }
        Text(
            when (node) {
                MapNode.LOOM -> "승인 전에는 정리 문서와 질문을 최대 3개 만들어요. 승인 뒤에는 Thread를 추출해 Fabric에 기록해요."
                MapNode.WIKI -> "오늘 만든 Digest와 항목별 Wiki 페이지예요. 폰에 도착한 결과를 Obsidian에서 열어요."
                MapNode.USER -> "나에게 승인을 기다리는 항목만 모았어요."
                else -> node.subtitle
            },
            style = MaterialTheme.typography.bodySmall,
        )
        if (node == MapNode.WIKI) {
            Text("오늘 · $today", style = MaterialTheme.typography.labelMedium)
            val digests =
                results
                    .mapNotNull { it.digest }
                    .filter { todayDigest(it, today) }
                    .distinctBy { it.path }
            if (digests.isEmpty())
                Text("오늘 Digest가 아직 없어요", style = MaterialTheme.typography.bodySmall)
            digests.forEach { page ->
                PageLink(page, "오늘 Digest", availability, vault) { message = it }
            }
            results.forEach { result ->
                if (result.wikiEdit != null) {
                    WikiEditCard(result, availability, vault) { message = it }
                    return@forEach
                }
                val todayPages = result.pages.filter { todayPage(it, today) }
                if (todayPages.isNotEmpty()) {
                    HorizontalDivider()
                    Text(
                        result.title.ifBlank { "저장한 항목" },
                        style = MaterialTheme.typography.titleSmall,
                    )
                    Text(
                        result.threads.joinToString(" · ") { "Thread · ${threadKindLabel(it.kind)}" },
                        style = MaterialTheme.typography.labelSmall,
                    )
                    todayPages.forEach { page ->
                        PageLink(
                            page,
                            page.path.removeSuffix(".md").substringAfterLast('/'),
                            availability,
                            vault,
                        ) {
                            message = it
                        }
                    }
                }
            }
            HorizontalDivider()
            Text("폰의 Obsidian 연결", style = MaterialTheme.typography.titleSmall)
            OutlinedTextField(
                vault,
                { vault = it },
                label = { Text("Obsidian vault 이름") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedButton(
                onClick = {
                    launcher.launch(
                        Intent(Intent.ACTION_OPEN_DOCUMENT_TREE).apply {
                            addFlags(
                                Intent.FLAG_GRANT_READ_URI_PERMISSION or
                                    Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION or
                                    Intent.FLAG_GRANT_PREFIX_URI_PERMISSION
                            )
                        }
                    )
                }
            ) {
                Text(if (folderSelected) "폰의 vault 폴더 다시 선택" else "폰에 동기화한 vault 폴더 선택")
            }
            Text(
                "이름과 폴더를 연결한 뒤, 갱신된 페이지가 폰에 도착하면 열 수 있어요.",
                style = MaterialTheme.typography.bodySmall,
            )
        } else {
            val here = items.filter { it.node == node && (node != MapNode.USER || it.needsReview) }
            if (node == MapNode.USER)
                Text("엮은 내용을 확인하고 승인해 주세요", style = MaterialTheme.typography.titleMedium)
            if (here.isEmpty())
                Text(
                    if (node == MapNode.USER) "지금 승인할 항목이 없어요" else "지금 여기 머무는 항목이 없어요",
                    style = MaterialTheme.typography.bodySmall,
                )
            here.sortedBy { if (it.id == focusedItemId) 0 else 1 }.forEach { item ->
                if (item.server?.wikiEdit != null) WikiEditCard(checkNotNull(item.server), availability, vault) { message = it }
                else if (node == MapNode.USER) ApprovalCard(item) { approval = item.packageId }
                else {
                    OutlinedCard(Modifier.fillMaxWidth()) {
                        Column(
                            Modifier.padding(14.dp),
                            verticalArrangement = Arrangement.spacedBy(6.dp),
                        ) {
                            Text(item.title, style = MaterialTheme.typography.titleSmall)
                            Text(
                                "${item.source} · ${item.stateLabel}",
                                style = MaterialTheme.typography.bodySmall,
                            )
                            if (item.stopped)
                                Text(
                                    "Mac에서 Weaver 로그를 확인해 주세요. 문제를 고친 뒤 다시 시도할 수 있어요.",
                                    color = MaterialTheme.colorScheme.error,
                                    style = MaterialTheme.typography.bodySmall,
                                )
                            item.server
                                ?.threads
                                ?.takeIf { it.isNotEmpty() }
                                ?.let { threads ->
                                    Text(
                                        threads.joinToString(" · ") { "Thread · ${threadKindLabel(it.kind)}" },
                                        style = MaterialTheme.typography.labelSmall,
                                    )
                                }
                        }
                    }
                }
            }
            approval?.let { id ->
                HorizontalDivider()
                ReviewPanel(id, onClose = { approval = null })
            }
        }
        message?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
    }
}

@Composable
internal fun PageLink(
    page: PipelinePage,
    label: String,
    availability: Set<String>,
    vault: String,
    onError: (String) -> Unit,
) {
    val context = LocalContext.current
    val arrived = page.path + ":" + page.sha256 in availability && vault.isNotBlank()
    TextButton(
        enabled = arrived,
        onClick = {
            runCatching {
                    context.startActivity(
                        Intent(
                            Intent.ACTION_VIEW,
                            android.net.Uri.parse(VaultAccess.obsidianUri(vault, page.path)),
                        )
                    )
                }
                .onFailure { onError("Obsidian을 열지 못했어요. 폰의 앱과 vault 이름을 확인해 주세요.") }
        },
        modifier = Modifier.fillMaxWidth(),
    ) {
        Text(if (arrived) "$label ↗" else "$label · 폰에 오는 중")
    }
}

private val stepGroups =
    listOf(
        listOf(Stage.COLLECT, Stage.SEND),
        listOf(Stage.ORGANIZE, Stage.QUESTION),
        listOf(Stage.REVIEW),
        listOf(Stage.APPLY, Stage.UPDATE_WIKI),
    )
private val stepNames = listOf("가져오기", "다듬기", "승인받기", "저장하기")

private fun stageName(stage: Stage) =
    when (stage) {
        Stage.COLLECT -> "수집"
        Stage.SEND -> "Mac 수신"
        Stage.ORGANIZE -> "정리"
        Stage.QUESTION -> "질문"
        Stage.REVIEW -> "승인 대기"
        Stage.APPLY -> "Fabric에 저장"
        Stage.UPDATE_WIKI -> "Wiki 갱신"
    }

@Composable
internal fun ApprovalCard(item: MapItem, onApprove: () -> Unit) {
    var expanded by rememberSaveable(item.id, item.packageRevision) { mutableStateOf(false) }
    OutlinedCard(Modifier.fillMaxWidth()) {
        Column(
            Modifier.fillMaxWidth().clickable { expanded = !expanded }.padding(14.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(item.title, style = MaterialTheme.typography.titleSmall)
            Text("${item.source} · ${item.stateLabel}", style = MaterialTheme.typography.bodySmall)
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                stepNames.forEachIndexed { index, name ->
                    Column(
                        Modifier.weight(1f),
                        horizontalAlignment = Alignment.CenterHorizontally,
                    ) {
                        Row(
                            Modifier.fillMaxWidth(),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            HorizontalDivider(
                                Modifier.weight(1f),
                                color =
                                    if (index < 2) MaterialTheme.colorScheme.primary
                                    else MaterialTheme.colorScheme.outlineVariant,
                            )
                            Box(
                                Modifier.size(10.dp)
                                    .background(
                                        if (index < 2) MaterialTheme.colorScheme.primary
                                        else if (index == 2) Color(0xffd78a26)
                                        else MaterialTheme.colorScheme.outlineVariant,
                                        CircleShape,
                                    )
                            )
                            HorizontalDivider(
                                Modifier.weight(1f),
                                color =
                                    if (index < 2) MaterialTheme.colorScheme.primary
                                    else MaterialTheme.colorScheme.outlineVariant,
                            )
                        }
                        Text(name, style = MaterialTheme.typography.labelSmall)
                    }
                }
            }
            Text(
                if (expanded) "단계 시각 접기" else "단계 시각 보기",
                style = MaterialTheme.typography.labelSmall,
            )
        }
        if (expanded)
            Column(
                Modifier.fillMaxWidth().padding(14.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                stepGroups.forEachIndexed { i, stages ->
                    Text(stepNames[i], style = MaterialTheme.typography.labelMedium)
                    val times =
                        stages.mapNotNull { stage ->
                            item.times[stage]?.let { "${stageName(stage)} · $it" }
                        }
                    Text(
                        if (times.isEmpty()) if (i == 3) "승인 후 시작" else "기록된 시각 없음"
                        else times.joinToString("\n"),
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
                Button(
                    onClick = onApprove,
                    enabled = item.packageId != null,
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Text("승인하러 가기")
                }
            }
    }
}
