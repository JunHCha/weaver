package io.github.junhcha.weaver.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import io.github.junhcha.weaver.phone.PendingRequest
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.produceState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.phone.Cached
import io.github.junhcha.weaver.phone.CompleteResult
import io.github.junhcha.weaver.phone.DoneStore
import io.github.junhcha.weaver.phone.LinkTarget
import io.github.junhcha.weaver.phone.ResultClient
import io.github.junhcha.weaver.schema.ResultCard
import io.github.junhcha.weaver.schema.TaskItem
import java.time.Instant
import java.time.ZoneId
import kotlinx.coroutines.CancellationException

/** Where a card leads: its document bullet, or the line of the day's Digest (the link's block). */
internal fun ResultCard.openTarget(): LinkTarget.Document? = link?.let { LinkTarget.Document(it.documentId, it.bulletId ?: it.blockId) }

/** Where a task leads: the line it was compiled into. */
internal fun TaskItem.openTarget(): LinkTarget.Document? = link?.let { LinkTarget.Document(it.documentId, it.bulletId ?: it.blockId) }

/**
 * The check flow of a task: hide it on this phone at once, ask the server to complete it, and bring it back only when the
 * server refuses (offline keeps it hidden; the journal sends it later).
 */
internal suspend fun completeTask(task: TaskItem, done: DoneStore, nowMs: Long, complete: suspend (String, Long) -> CompleteResult): CompleteResult {
    done.hide(task.threadId, nowMs)
    // A cancelled flow may already have journaled the request: the mark stays and DoneStore.reconcile decides later.
    val result = try { complete(task.threadId, task.revision) }
    catch (error: CancellationException) { throw error }
    catch (error: Exception) { CompleteResult.Rejected("complete_failed") }
    if (result is CompleteResult.Rejected) done.unhide(task.threadId)
    else done.hide(task.threadId, nowMs) // a refresh in between may have reconciled an older refused attempt away
    return result
}

/** 되돌리기 of a completion: asks the server to undo it and shows the task again; the error line when that failed. */
internal suspend fun undoCompletion(task: TaskItem, operation: PendingRequest, done: DoneStore, undo: suspend (PendingRequest) -> Unit): String? =
    try { undo(operation); done.unhide(task.threadId); null }
    catch (error: CancellationException) { throw error }
    catch (error: Exception) { "되돌리지 못했어요" }

/** The five second toast after a completed check. */
@Composable
internal fun CompleteToast(title: String, onUndo: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp).background(WeaverTokens.ink, RoundedCornerShape(WeaverTokens.number("radius.card").dp))
            .semantics { liveRegion = LiveRegionMode.Polite }.testTag("complete-toast"),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text("완료함 · $title", Modifier.weight(1f).padding(start = 12.dp), style = MaterialTheme.typography.labelMedium,
            color = WeaverTokens.surface, maxLines = 1, overflow = TextOverflow.Ellipsis)
        TextButton(onClick = onUndo, Modifier.defaultMinSize(minHeight = 48.dp).testTag("complete-undo")) { Text("되돌리기", color = WeaverTokens.surface) }
    }
}

/** The error line of a refused completion; null when nothing needs saying. */
internal fun completeProblem(result: CompleteResult): String? = (result as? CompleteResult.Rejected)?.let { "완료하지 못했어요 (${it.code})" }

/**
 * Loads the tasks and the cards around [at] (the time the screen was opened) and shows them as [NowResults]. The tasks the
 * person checked stay hidden while the server still lists them; once a fresh answer no longer lists one, [done] forgets it.
 * A copy kept for offline use never clears a mark. [reload] re-reads everything.
 */
@Composable
internal fun NowResultsContent(
    results: ResultClient,
    done: DoneStore,
    at: Instant,
    zone: ZoneId,
    reload: Int,
    onCheck: (TaskItem) -> Unit,
    onCard: (ResultCard) -> Unit,
    onTask: (TaskItem) -> Unit,
) {
    val tasks by produceState<Cached<List<TaskItem>>?>(null, results, reload) {
        value = results.tasks().also { read ->
            if (read.offlineSince == null && !read.unavailable) done.prune(read.value.map { it.threadId }.toSet())
        }
    }
    val today by produceState<Cached<List<ResultCard>>?>(null, results, at, reload) { value = results.cards("today", at.toString()) }
    val around by produceState<Cached<List<ResultCard>>?>(null, results, at, reload) { value = results.cards("around", at.toString()) }
    val hidden by done.ids.collectAsState()
    val loaded = listOfNotNull(tasks, today, around)
    if (loaded.size < 3) { Muted("불러오는 중", Modifier.testTag("now-results-loading")); return }
    val visible = tasks!!.value.filter { it.threadId !in hidden }
    if (visible.isEmpty() && today!!.value.isEmpty() && around!!.value.isEmpty()) {
        if (loaded.any { it.unavailable }) Muted("불러오지 못했어요. 연결을 확인해 주세요.", Modifier.testTag("now-results-unavailable"))
        else Muted("지금 처리할 일이 없어요", Modifier.testTag("now-results-empty"))
        return
    }
    loaded.firstNotNullOfOrNull { it.offlineSince }?.let { Muted("저장된 사본", Modifier.testTag("now-results-stale")) }
    NowResults(visible, today!!.value, around!!.value, onCheck, onCard, onTask, zone, at)
}

/**
 * 내 남은 일, 오늘 들어온 일 and 앞뒤 7일, in that order; a section without rows is left out. Every number and date word is the
 * server's (`due_label`); nothing is counted down here. [at] only names the 7 day window of the last header.
 */
@Composable
fun NowResults(
    tasks: List<TaskItem>,
    today: List<ResultCard>,
    around: List<ResultCard>,
    onCheck: (TaskItem) -> Unit,
    onCard: (ResultCard) -> Unit,
    onTask: (TaskItem) -> Unit,
    zone: ZoneId,
    at: Instant = Instant.now(),
) {
    Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        if (tasks.isNotEmpty()) Column(Modifier.testTag("now-tasks"), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            ResultsHeading("내 남은 일 ${tasks.size}", "체크하면 완료로 기록")
            tasks.forEach { key(it.threadId) { TaskRow(it, onCheck, onTask) } }
        }
        if (today.isNotEmpty()) Column(Modifier.testTag("now-today"), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            val calls = today.sumOf { c -> c.channels.count { it == "통화" } }
            val shares = today.sumOf { c -> c.channels.count { it == "공유" } }
            ResultsHeading("오늘 들어온 일", if (calls + shares > 0) "통화 $calls · 공유 $shares" else null)
            today.forEach { key(it.cardId) { CardRow(it, showDue = false, onCard) } }
        }
        if (around.isNotEmpty()) Column(Modifier.testTag("now-around"), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            val day = at.atZone(zone).toLocalDate()
            ResultsHeading("앞뒤 7일", "${day.minusDays(7).shortDate()} – ${day.plusDays(7).shortDate()}")
            around.forEach { key(it.cardId) { CardRow(it, showDue = true, onCard) } }
        }
    }
}

@Composable
private fun ResultsHeading(title: String, trailing: String?) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.Bottom) {
        SectionHeading(title)
        if (trailing != null) Muted(trailing, Modifier.weight(1f).padding(start = 8.dp, bottom = 4.dp))
    }
}

@Composable
private fun DueLabel(label: String) {
    val today = label == "오늘" // indigo and bold: 홍화 is the time thread only
    Text(label, style = MaterialTheme.typography.labelMedium.let { if (today) it.copy(fontWeight = FontWeight.Bold) else it },
        color = if (today) WeaverTokens.indigo else WeaverTokens.muted)
}

@Composable
private fun TaskRow(task: TaskItem, onCheck: (TaskItem) -> Unit, onTask: (TaskItem) -> Unit) {
    Row(Modifier.fillMaxWidth().defaultMinSize(minHeight = 48.dp).testTag("task-${task.threadId}"), verticalAlignment = Alignment.CenterVertically) {
        Box(
            Modifier.size(48.dp)
                .toggleable(value = false, role = Role.Checkbox, onValueChange = { onCheck(task) })
                .semantics { stateDescription = "완료 안 됨"; contentDescription = "${task.content} 완료로 기록" }
                .testTag("task-check-${task.threadId}"),
            contentAlignment = Alignment.Center,
        ) { Box(Modifier.size(22.dp).border(2.dp, WeaverTokens.muted, RoundedCornerShape(4.dp))) }
        Column(Modifier.weight(1f).clickable(role = Role.Button) { onTask(task) }.defaultMinSize(minHeight = 48.dp).padding(vertical = 6.dp),
            verticalArrangement = Arrangement.Center) {
            Text(task.content, style = MaterialTheme.typography.bodyLarge)
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                task.documentTitle?.let { Muted(it) }
                task.dueLabel?.let { DueLabel(it) }
            }
        }
    }
}

@Composable
private fun CardRow(card: ResultCard, showDue: Boolean, onCard: (ResultCard) -> Unit) {
    KindCard(Modifier.testTag("now-card-${card.cardId}").defaultMinSize(minHeight = 48.dp).clickable(role = Role.Button) { onCard(card) }) {
        Column(Modifier.padding(horizontal = 12.dp, vertical = 8.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                if (showDue) card.dueLabel?.let { DueLabel(it) }
                Text(card.headline, Modifier.weight(1f), style = MaterialTheme.typography.titleSmall, maxLines = 2, overflow = TextOverflow.Ellipsis)
                if (card.reflecting) Text("반영중", Modifier.testTag("now-card-reflecting-${card.cardId}")
                    .border(1.dp, WeaverTokens.line, RoundedCornerShape(6.dp)).padding(horizontal = 6.dp, vertical = 2.dp),
                    style = MaterialTheme.typography.labelSmall, color = WeaverTokens.indigo)
            }
            card.bullets.take(2).forEach { Text(it, style = MaterialTheme.typography.bodyMedium, maxLines = 2, overflow = TextOverflow.Ellipsis) }
            val meta = listOfNotNull(card.counterparts.takeIf { it.isNotEmpty() }?.joinToString(", "), card.channels.takeIf { it.isNotEmpty() }?.let { "채널 ${it.size}" })
            if (meta.isNotEmpty()) Muted(meta.joinToString(" · "))
        }
    }
}
