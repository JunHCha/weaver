package io.github.junhcha.weaver.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.phone.Direction
import io.github.junhcha.weaver.phone.PhoneThread
import io.github.junhcha.weaver.phone.DueKind
import io.github.junhcha.weaver.phone.NowEntry
import io.github.junhcha.weaver.phone.NowView
import io.github.junhcha.weaver.phone.SeoulZone
import io.github.junhcha.weaver.phone.StatusCounts
import java.time.Instant

/** "지금": only what needs deciding today. The pipeline map shrinks to the status strip; tapping it opens the map. */
@Composable
internal fun NowScreen(
    view: NowView?,
    counts: StatusCounts,
    now: Instant,
    onOpenMap: () -> Unit,
    onOpenSettings: () -> Unit,
) {
    val today = now.atZone(SeoulZone).toLocalDate()
    Column(Modifier.fillMaxSize().testTag("now-screen")) {
        Row(Modifier.fillMaxWidth().padding(start = 16.dp, end = 4.dp, top = 4.dp), verticalAlignment = Alignment.CenterVertically) {
            Text("지금", style = MaterialTheme.typography.titleLarge, color = WeaverTokens.indigo)
            Muted("${today.shortDate()} (${today.dayOfWeekKo()})", Modifier.weight(1f).padding(start = 8.dp))
            TextButton(onClick = onOpenSettings) { Text("수집·설정") }
        }
        StatusStrip(counts, onOpenMap)
        Column(
            Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(horizontal = 16.dp, vertical = 4.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            when {
                view == null -> Muted("불러오는 중")
                view.isEmpty -> Muted("지금 처리할 일이 없어요")
                else -> {
                    NowSection("기한 지남", view.overdue) { entry ->
                        "마감 ${entry.thread.deadline?.shortDate()} · ${entry.thread.typeLabel()} · ${entry.thread.counterpart.orEmpty()}"
                    }
                    NowSection("오늘", view.today) { entry ->
                        val time = if (entry.dueKind == DueKind.SCHEDULE) entry.thread.scheduleAt?.clock() else "마감 오늘"
                        listOfNotNull(time, entry.thread.typeLabel(), entry.thread.tags.firstOrNull().takeIf { entry.dueKind == DueKind.SCHEDULE } ?: entry.thread.counterpart)
                            .joinToString(" · ")
                    }
                    NowSection("새로 반영됨", view.recent) { entry ->
                        val at = entry.thread.recordedAt
                        val day = if (at.atZone(SeoulZone).toLocalDate() == today) "오늘" else if (at.atZone(SeoulZone).toLocalDate() == today.minusDays(1)) "어제" else at.shortDate()
                        "$day ${at.clock()} · ${entry.thread.kind.label} · ${entry.thread.source}"
                    }
                }
            }
        }
    }
}

@Composable
private fun StatusStrip(counts: StatusCounts, onOpen: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp).testTag("status-strip")
            .clickable(role = Role.Button, onClickLabel = "데이터 흐름 지도 열기", onClick = onOpen),
        horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        StatusCell("검토 대기", counts.waitingReview, Modifier.weight(1f))
        StatusCell("처리 중", counts.processing, Modifier.weight(1f))
        StatusCell("중지", counts.stopped, Modifier.weight(1f))
    }
}

@Composable
private fun StatusCell(label: String, count: Int, modifier: Modifier) {
    Surface(
        modifier, shape = RoundedCornerShape(WeaverTokens.number("radius.card").dp), color = WeaverTokens.surface,
        border = BorderStroke(1.dp, WeaverTokens.line),
    ) {
        Row(Modifier.padding(horizontal = 8.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
            Text("$count", style = MaterialTheme.typography.labelLarge, color = WeaverTokens.ink)
            Text(label, Modifier.padding(start = 4.dp), style = MaterialTheme.typography.labelMedium, color = WeaverTokens.muted)
        }
    }
}

@Composable
private fun NowSection(title: String, entries: List<NowEntry>, detail: (NowEntry) -> String) {
    if (entries.isEmpty()) return
    SectionHeading(title)
    entries.forEach { ThreadCard(it.thread, detail(it)) }
}

/** "받은 요청" / "보낸 요청" when the direction is known, otherwise the plain kind. */
private fun PhoneThread.typeLabel(): String = when (direction) {
    Direction.RECEIVED -> "받은 요청"
    Direction.SENT -> "보낸 요청"
    null -> kind.label
}
