package io.github.junhcha.weaver.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.produceState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.phone.Cached
import io.github.junhcha.weaver.phone.ResultClient
import io.github.junhcha.weaver.schema.RefLinkView
import io.github.junhcha.weaver.schema.TaskItem
import io.github.junhcha.weaver.schema.TreeNodeView

/** A tree node and how deep it sits (0 = 생활권). */
data class AreaRow(val node: TreeNodeView, val depth: Int)

/**
 * Flattens the server's nodes into display order: every node follows its parent, siblings keep the server's order.
 * A node whose parent is not in the list is shown as a root, so a partial tree never hides anything.
 */
fun areaRows(nodes: List<TreeNodeView>): List<AreaRow> {
    val paths = nodes.mapTo(HashSet()) { it.path }
    val children = nodes.groupBy { it.parent?.takeIf { p -> p in paths } }
    val rows = mutableListOf<AreaRow>()
    val seen = HashSet<String>()
    fun walk(parent: String?, depth: Int) {
        for (node in children[parent].orEmpty()) {
            if (!seen.add(node.path)) continue
            rows += AreaRow(node, depth)
            walk(node.path, depth + 1)
        }
    }
    walk(null, 0)
    return rows
}

/**
 * 생활권 -> 영역 -> 대상·프로젝트 indented. A row reads `label · 남은 일 N` and carries a "문서" mark when the node has a
 * document; [onNode] opens the node, [onDocument] the mark's document.
 */
@Composable
fun AreaTree(
    nodes: List<TreeNodeView>,
    onNode: (TreeNodeView) -> Unit,
    onDocument: (String) -> Unit,
    selected: String? = null,
    modifier: Modifier = Modifier,
) {
    val rows = areaRows(nodes)
    LazyColumn(modifier.testTag("area-tree")) {
        items(rows, key = { it.node.path }) { (node, depth) ->
            Row(
                Modifier.fillMaxWidth().heightIn(min = 48.dp)
                    .background(if (node.path == selected) WeaverTokens.selected else androidx.compose.ui.graphics.Color.Transparent)
                    .clickable(role = Role.Button) { onNode(node) }
                    .padding(start = (16 + depth * 16).dp, end = 8.dp)
                    .testTag("area-node-${node.path}"),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    "${node.label} · 남은 일 ${node.openTasks}", Modifier.weight(1f).padding(vertical = 8.dp),
                    style = MaterialTheme.typography.bodyLarge.let { if (depth == 0) it.copy(fontWeight = FontWeight.SemiBold) else it },
                )
                node.documentId?.let { id ->
                    TextButton(
                        onClick = { onDocument(id) },
                        Modifier.defaultMinSize(minWidth = 48.dp, minHeight = 48.dp).testTag("area-doc-${node.path}"),
                    ) { Text("문서", color = WeaverTokens.indigo) }
                }
            }
        }
    }
}

/** A node without a document: its name and the notes related to it. */
@Composable
fun AreaNodeDetail(node: TreeNodeView, references: Cached<List<RefLinkView>>?, onBack: () -> Unit) {
    Column(Modifier.fillMaxSize().testTag("area-node-detail")) {
        Row(Modifier.fillMaxWidth().padding(end = 16.dp), verticalAlignment = Alignment.CenterVertically) {
            TextButton(onClick = onBack, Modifier.defaultMinSize(minWidth = 48.dp, minHeight = 48.dp).semantics { contentDescription = "뒤로" }) { Text("‹") }
        }
        Column(Modifier.verticalScroll(rememberScrollState()).padding(horizontal = 16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(node.label, style = MaterialTheme.typography.titleLarge, color = WeaverTokens.indigo)
            Muted("남은 일 ${node.openTasks} · 최근 7일 카드 ${node.cards7d}")
            SectionHeading("관련 자료")
            when {
                references == null -> Muted("불러오는 중")
                references.unavailable -> Muted("불러오지 못했어요", Modifier.testTag("node-references-unavailable"))
                references.offlineSince != null -> Muted("저장된 사본이에요", Modifier.testTag("node-references-stale"))
                references.value.isEmpty() -> Muted("아직 없어요")
            }
            references?.value.orEmpty().forEach { r ->
                Column(Modifier.fillMaxWidth().padding(vertical = 6.dp).testTag("node-reference-${r.path}")) {
                    Text(r.heading, style = MaterialTheme.typography.bodyLarge)
                    Muted(listOf(r.sphere, r.path).plus(if (r.home) listOf("홈") else emptyList()).joinToString(" · "))
                }
            }
        }
    }
}

/**
 * The 영역 tab when the server has `results`: the tree on the left, and on the right the document of the chosen node
 * (with its evidence) or, for a node without one, its related notes. The selection is owned by the caller.
 */
@Composable
fun AreasTab(
    results: ResultClient,
    wide: Boolean,
    path: String?,
    documentId: String?,
    bulletId: String?,
    focus: String?,
    reload: Int,
    onPath: (String?) -> Unit,
    onDocument: (id: String, path: String?) -> Unit,
    onBullet: (String?) -> Unit,
    onTask: (TaskItem) -> Unit,
    onLog: (String) -> Unit,
    onCloseDocument: () -> Unit,
    doneIds: Set<String> = emptySet(),
    feedbackFirst: Boolean = false,
) {
    val tree by produceState<Cached<List<TreeNodeView>>?>(null, results, reload) { value = results.tree() }
    val nodes = tree?.value.orEmpty()
    val node = path?.let { p -> nodes.firstOrNull { it.path == p } }
    TwoPane(
        list = {
            Column(Modifier.fillMaxSize().testTag("area-tree-list")) {
                ScreenTitle("영역")
                if (tree == null) Muted("불러오는 중", Modifier.padding(horizontal = 16.dp))
                else {
                    val loaded = checkNotNull(tree)
                    if (loaded.unavailable) Muted("불러오지 못했어요. 연결을 확인해 주세요.", Modifier.padding(horizontal = 16.dp).testTag("area-tree-unavailable"))
                    else {
                        loaded.offlineSince?.let { Muted("저장된 사본", Modifier.padding(horizontal = 16.dp)) }
                        if (nodes.isEmpty()) Muted("아직 없어요", Modifier.padding(horizontal = 16.dp))
                    }
                    AreaTree(nodes, { n -> onPath(n.path); n.documentId?.let { onDocument(it, n.path) } }, { id -> onDocument(id, nodes.firstOrNull { it.documentId == id }?.path) }, path)
                }
            }
        },
        detail = when {
            documentId != null -> ({
                DocumentRoute(results, documentId, path, bulletId, onBullet, onTask, onLog, focus, wide, onCloseDocument, reload, doneIds, feedbackFirst)
            })
            node != null -> ({
                val refs by produceState<Cached<List<RefLinkView>>?>(null, results, node.path) { value = results.references(node.path) }
                AreaNodeDetail(node, refs) { onPath(null) }
            })
            else -> null
        },
        emptyDetail = "영역이나 문서를 선택하세요", wide = wide,
    )
}
