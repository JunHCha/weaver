package io.github.junhcha.weaver.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.phone.DictionaryEntry
import io.github.junhcha.weaver.phone.PhoneTag
import io.github.junhcha.weaver.phone.PhoneThread
import io.github.junhcha.weaver.phone.TagEditResult
import io.github.junhcha.weaver.phone.TagEditor
import io.github.junhcha.weaver.phone.TagKind
import io.github.junhcha.weaver.phone.suggestTags
import io.github.junhcha.weaver.phone.tagEditable
import io.github.junhcha.weaver.transport.ApiException
import io.github.junhcha.weaver.transport.UnreachableException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** Where tag edits go. A seam so screens can be tested without a server. */
interface TagEditHost {
    suspend fun dictionary(): List<DictionaryEntry>
    suspend fun editTags(threadIds: List<String>, add: List<String>, remove: List<String>, kinds: Map<String, TagKind>, key: String): TagEditResult
    suspend fun rename(entry: String, name: String, key: String)
    suspend fun setKind(entry: String, kind: TagKind, key: String)
    suspend fun addAlias(entry: String, alias: String, key: String)
    suspend fun removeAlias(entry: String, alias: String, key: String)
    suspend fun merge(entry: String, target: String, kind: TagKind?, key: String)
}

class ApiTagEditHost(private val editor: TagEditor) : TagEditHost {
    private suspend fun <T> io(block: () -> T): T = withContext(Dispatchers.IO) { block() }
    override suspend fun dictionary() = io { editor.dictionary() }
    override suspend fun editTags(threadIds: List<String>, add: List<String>, remove: List<String>, kinds: Map<String, TagKind>, key: String) = io {
        if (threadIds.size == 1) editor.editThread(threadIds.single(), add, remove, kinds, key)
        else editor.editBatch(threadIds, add, remove, kinds, key)
    }
    override suspend fun rename(entry: String, name: String, key: String) = io { editor.rename(entry, name, key) }
    override suspend fun setKind(entry: String, kind: TagKind, key: String) = io { editor.setKind(entry, kind, key) }
    override suspend fun addAlias(entry: String, alias: String, key: String) = io { editor.addAlias(entry, alias, key) }
    override suspend fun removeAlias(entry: String, alias: String, key: String) = io { editor.removeAlias(entry, alias, key) }
    override suspend fun merge(entry: String, target: String, kind: TagKind?, key: String) = io { editor.merge(entry, target, kind, key) }
}

/** User-facing text for a failed edit; never a server message, only what the fixed codes mean. */
internal fun editErrorText(error: Throwable): String = when (error) {
    is UnreachableException -> "서버에 연결할 수 없어요. 연결 후 다시 시도해 주세요"
    is ApiException -> when (error.status) {
        401, 403 -> "서버가 이 폰을 거절했어요. 설정에서 다시 연결해 주세요"
        404 -> "대상을 찾을 수 없어요"
        409, 412 -> "그 사이 내용이 바뀌었어요. 새로 고친 뒤 다시 해 주세요"
        422 -> "이 변경은 허용되지 않아요"
        else -> "저장하지 못했어요 (${error.code})"
    }
    else -> "저장하지 못했어요"
}

/**
 * Bottom sheet for one Thread, or several at once. Tag chips are added with a dictionary search and removed with
 * the chip's own toggle; history rows are refused with a note instead of an editor.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun TagEditSheet(
    threads: List<PhoneThread>, tags: List<PhoneTag>, host: TagEditHost, onDone: (changed: Boolean) -> Unit,
) {
    ModalBottomSheet(onDismissRequest = { onDone(false) }) {
        var entries by remember { mutableStateOf<List<DictionaryEntry>>(emptyList()) }
        var saving by remember { mutableStateOf(false) }
        var error by remember { mutableStateOf<String?>(null) }
        // One key per user action: pressing 저장 again after a lost response must not edit twice.
        val key = remember { java.util.UUID.randomUUID().toString() }
        val scope = rememberCoroutineScope()
        LaunchedEffect(host) { entries = runCatching { host.dictionary() }.getOrDefault(emptyList()) }
        TagEditContent(threads, tags, entries, saving, error, onCancel = { onDone(false) }) { add, remove, kinds ->
            saving = true; error = null
            scope.launch {
                runCatching { host.editTags(threads.filter { it.tagEditable() }.map { it.id }, add, remove, kinds, key) }
                    .onSuccess { onDone(true) }
                    .onFailure { error = editErrorText(it); saving = false }
            }
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
internal fun TagEditContent(
    threads: List<PhoneThread>, tags: List<PhoneTag>, entries: List<DictionaryEntry>, saving: Boolean, error: String?,
    onCancel: () -> Unit, onSave: (add: List<String>, remove: List<String>, kinds: Map<String, TagKind>) -> Unit,
) {
    val editable = threads.filter { it.tagEditable() }
    Column(Modifier.padding(horizontal = 16.dp).padding(bottom = 16.dp).testTag("tag-sheet"), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(
            if (threads.size == 1) "태그 편집" else "${threads.size}개 Thread 태그 편집",
            style = MaterialTheme.typography.titleMedium, color = WeaverTokens.indigo,
        )
        if (editable.isEmpty()) {
            Muted("이력 Thread는 편집할 수 없어요. 대상이 되는 원래 Thread에서 고쳐 주세요.", Modifier.testTag("tag-history-note"))
            TextButton(onClick = onCancel, Modifier.defaultMinSize(minHeight = 48.dp)) { Text("닫기") }
            return@Column
        }
        if (editable.size < threads.size) Muted("이력 Thread ${threads.size - editable.size}개는 빼고 편집해요")
        Muted(editable.first().content.let { if (editable.size == 1) it else "${editable.size}개 선택됨" }, Modifier.testTag("tag-target"))

        val counts = editable.flatMap { it.tags.distinct() }.groupingBy { it }.eachCount()
        val removed = remember { mutableStateListOf<String>() }
        val added = remember { mutableStateListOf<String>() }
        val kinds = remember { mutableStateOf<Map<String, TagKind>>(emptyMap()) }
        var query by remember { mutableStateOf("") }
        var newKind by remember { mutableStateOf(TagKind.TOPIC) }

        Muted("붙어 있는 태그 (누르면 뗌)")
        FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            if (counts.isEmpty()) Muted("없음")
            counts.keys.sorted().forEach { name ->
                val partial = counts.getValue(name) < editable.size
                val off = name in removed
                PhoneChip(
                    (if (off) "− " else "") + name + if (partial) " ${counts.getValue(name)}/${editable.size}" else "",
                    selected = !off, onClick = { if (off) removed.remove(name) else removed.add(name) },
                    modifier = Modifier.testTag("tag-chip-$name"),
                )
            }
            added.forEach { name ->
                PhoneChip("+ $name", selected = true, onClick = { added.remove(name) }, modifier = Modifier.testTag("tag-added-$name"))
            }
        }
        OutlinedTextField(
            query, { query = it }, Modifier.fillMaxWidth().testTag("tag-search"), singleLine = true,
            label = { Text("태그 검색") }, keyboardOptions = KeyboardOptions.Default,
        )
        val taken = counts.filterValues { it == editable.size }.keys + added
        val hits = suggestTags(query, tags, entries, taken)
        FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            hits.forEach { tag ->
                PhoneChip(tag.name + (tag.kind?.let { " · ${it.label}" } ?: ""), selected = false, onClick = {
                    removed.remove(tag.name)
                    if (tag.name !in counts.keys || counts.getValue(tag.name) < editable.size) added.add(tag.name)
                    query = ""
                }, modifier = Modifier.testTag("tag-hit-${tag.name}"))
            }
        }
        val fresh = query.trim()
        if (fresh.isNotEmpty() && hits.none { it.name == fresh } && tags.none { it.name == fresh } && fresh !in added) {
            Muted("‘$fresh’ 새 태그의 종류")
            FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                TagKind.entries.forEach { PhoneChip(it.label, newKind == it, { newKind = it }) }
            }
            OutlinedButton(onClick = {
                added.add(fresh); kinds.value = kinds.value + (fresh to newKind); query = ""
            }, Modifier.defaultMinSize(minHeight = 48.dp).testTag("tag-create")) { Text("＋ ‘$fresh’ 만들어 붙이기") }
        }
        if (error != null) Text(error, color = WeaverTokens.thread, style = MaterialTheme.typography.bodySmall, modifier = Modifier.testTag("tag-error"))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(onClick = onCancel, Modifier.defaultMinSize(minHeight = 48.dp)) { Text("취소") }
            Button(
                onClick = { onSave(added.toList(), removed.toList(), kinds.value.filterKeys { it in added }) },
                enabled = !saving && (added.isNotEmpty() || removed.isNotEmpty()),
                modifier = Modifier.defaultMinSize(minHeight = 48.dp).testTag("tag-save"),
            ) { Text(if (saving) "저장 중" else "저장") }
        }
    }
}

/** Light dictionary editing for one tag: rename, kind, aliases, merge into another. Each action is its own request. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun DictionarySheet(name: String, tags: List<PhoneTag>, host: TagEditHost, onDone: (changed: Boolean) -> Unit) {
    ModalBottomSheet(onDismissRequest = { onDone(false) }) {
        var entry by remember { mutableStateOf<DictionaryEntry?>(null) }
        var changed by remember { mutableStateOf(false) }
        var error by remember { mutableStateOf<String?>(null) }
        var busy by remember { mutableStateOf(false) }
        val scope = rememberCoroutineScope()
        var current by remember { mutableStateOf(name) }
        suspend fun reload() { entry = runCatching { host.dictionary() }.getOrDefault(emptyList()).firstOrNull { it.name == current } }
        LaunchedEffect(host) { reload() }
        fun run(rename: String? = null, action: suspend () -> Unit) {
            busy = true; error = null
            scope.launch {
                runCatching { action() }.onSuccess { changed = true; if (rename != null) current = rename; reload() }
                    .onFailure { error = editErrorText(it) }
                busy = false
            }
        }
        DictionaryContent(current, entry, tags, busy, error, onClose = { onDone(changed) },
            onRename = { n -> run(n) { host.rename(current, n, java.util.UUID.randomUUID().toString()) } },
            onKind = { k -> run { host.setKind(current, k, java.util.UUID.randomUUID().toString()) } },
            onAddAlias = { a -> run { host.addAlias(current, a, java.util.UUID.randomUUID().toString()) } },
            onRemoveAlias = { a -> run { host.removeAlias(current, a, java.util.UUID.randomUUID().toString()) } },
            onMerge = { target, kind -> run { host.merge(current, target, kind, java.util.UUID.randomUUID().toString()); onDone(true) } },
        )
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
internal fun DictionaryContent(
    name: String, entry: DictionaryEntry?, tags: List<PhoneTag>, busy: Boolean, error: String?, onClose: () -> Unit,
    onRename: (String) -> Unit, onKind: (TagKind) -> Unit, onAddAlias: (String) -> Unit, onRemoveAlias: (String) -> Unit,
    onMerge: (String, TagKind?) -> Unit,
) {
    var newName by remember(name) { mutableStateOf("") }
    var alias by remember(name) { mutableStateOf("") }
    var target by remember(name) { mutableStateOf("") }
    Column(
        Modifier.padding(horizontal = 16.dp).padding(bottom = 16.dp).verticalScroll(rememberScrollState()).testTag("dictionary-sheet"),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text("사전: $name", style = MaterialTheme.typography.titleMedium, color = WeaverTokens.indigo)
        if (entry == null) Muted("사전에 없는 태그예요. 이름 변경과 합치기는 사전 항목에서만 돼요.")
        Muted("종류")
        FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            TagKind.entries.forEach { PhoneChip(it.label, entry?.kind == it, { if (!busy) onKind(it) }) }
        }
        Muted("별칭")
        FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            if (entry?.aliases.isNullOrEmpty()) Muted("없음")
            entry?.aliases?.forEach { PhoneChip("$it ✕", true, { if (!busy) onRemoveAlias(it) }) }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedTextField(alias, { alias = it }, Modifier.weight(1f), singleLine = true, label = { Text("별칭 추가") })
            OutlinedButton(onClick = { onAddAlias(alias.trim()); alias = "" }, enabled = !busy && alias.isNotBlank(),
                modifier = Modifier.defaultMinSize(minHeight = 48.dp)) { Text("추가") }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedTextField(newName, { newName = it }, Modifier.weight(1f), singleLine = true, label = { Text("새 이름") })
            OutlinedButton(onClick = { onRename(newName.trim()); newName = "" }, enabled = !busy && newName.isNotBlank(),
                modifier = Modifier.defaultMinSize(minHeight = 48.dp)) { Text("변경") }
        }
        Muted("다른 태그로 합치기 (이 태그가 사라져요)")
        OutlinedTextField(target, { target = it }, Modifier.fillMaxWidth(), singleLine = true, label = { Text("합칠 태그 검색") })
        FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            suggestTags(target, tags, emptyList(), setOf(name)).forEach { hit ->
                PhoneChip("→ ${hit.name}", false, {
                    if (!busy) onMerge(hit.name, if (hit.kind != null && entry?.kind != null && hit.kind != entry.kind) hit.kind else null)
                })
            }
        }
        if (error != null) Text(error, color = WeaverTokens.thread, style = MaterialTheme.typography.bodySmall)
        TextButton(onClick = onClose, Modifier.defaultMinSize(minHeight = 48.dp)) { Text("닫기") }
    }
}
