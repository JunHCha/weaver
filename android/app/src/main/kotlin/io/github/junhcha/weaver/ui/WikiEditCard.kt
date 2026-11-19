package io.github.junhcha.weaver.ui

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.schema.*
@Composable
internal fun WikiEditCard(
    item: PipelineItem, availability: Set<String>, vault: String,
    showMetadata: Boolean = false,
    subjectTarget: (String, String?) -> io.github.junhcha.weaver.phone.LinkTarget = { _, _ -> io.github.junhcha.weaver.phone.LinkTarget.None },
    onOpenSubject: (io.github.junhcha.weaver.phone.LinkTarget) -> Unit = {},
    onError: (String) -> Unit,
) {
    val edit = checkNotNull(item.wikiEdit)
    var selected by rememberSaveable(item.sourceId) { mutableIntStateOf(0) }
    val outputs = item.pages + listOfNotNull(item.digest)
    val arrived = outputs.isNotEmpty() && vault.isNotBlank() &&
        outputs.all { it.path + ":" + it.sha256 in availability }
    KindCard(kind = item.threads.cardKind()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("Wiki에서 시작 · 사람 직접 수정", color = wikiEditColor, style = MaterialTheme.typography.labelMedium)
            Text(sourceTitle(item.title), style = MaterialTheme.typography.titleMedium)
            if (showMetadata) FabricCardInfo(item, subjectTarget, onOpenSubject)
            Text(displayName(edit.path), style = MaterialTheme.typography.bodySmall)
            Text(when (edit.state) {
                "settling" -> if (edit.readyAt == null) "수정 내용 확인 중" else "수정 감지 · 편집 안정 대기"
                "recorded" -> "Fabric 반영 · Wiki 갱신 중"
                "published" -> if (arrived) "Wiki 갱신 완료 · 폰 도착 확인" else "Wiki 갱신 완료 · 폰에 오는 중"
                else -> "추가할 사실 없음 · 편집 이력 보관"
            }, style = MaterialTheme.typography.bodySmall)
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                listOf("수정 내역", "Thread ${item.threads.size}", "결과물").forEachIndexed { index, title ->
                    TextButton(onClick = { selected = index }) { Text(title, color = if (selected == index) wikiEditColor else MaterialTheme.colorScheme.onSurfaceVariant) }
                }
            }
            when (selected) {
                0 -> {
                    if (edit.changes.isEmpty()) Text(if (edit.state == "unchanged") "오타·서식 수정이나 원상복구로 추가할 사실이 없어요." else "변경 내용을 확인하고 있어요.", style = MaterialTheme.typography.bodySmall)
                    edit.changes.forEach { change ->
                        Text(when (change.field) {
                            "deadline" -> "마감 변경"
                            "schedule" -> "일정 변경"
                            "assignees" -> "담당자 변경"
                            "status" -> "상태 변경"
                            else -> "본문 변경"
                        }, style = MaterialTheme.typography.labelMedium)
                        change.before?.let { Text(it, textDecoration = TextDecoration.LineThrough, color = MaterialTheme.colorScheme.onSurfaceVariant, style = MaterialTheme.typography.bodySmall) }
                        Text(change.after ?: "값 삭제", style = MaterialTheme.typography.bodySmall)
                    }
                    HorizontalDivider()
                    edit.detectedAt?.let { Text("Wiki 수정 감지 · ${mapTime(it)}", style = MaterialTheme.typography.bodySmall) }
                    edit.readyAt?.let { Text("편집 안정 대기 · ${mapTime(it)} 이후 반영 가능", style = MaterialTheme.typography.bodySmall) }
                    item.stageTimes[Stage.APPLY]?.let { Text("Fabric 반영 · ${mapTime(it)}", style = MaterialTheme.typography.bodySmall) }
                    item.stageTimes[Stage.UPDATE_WIKI]?.let { Text("Wiki 갱신 · ${mapTime(it)}", style = MaterialTheme.typography.bodySmall) }
                    if (edit.state == "published") Text(if (arrived) "폰 도착 확인" else "폰에 오는 중 · 최신 파일 일치 확인 대기", style = MaterialTheme.typography.bodySmall)
                }
                1 -> {
                    if (item.threads.isEmpty()) Text("아직 발행된 Thread가 없어요.", style = MaterialTheme.typography.bodySmall)
                    ThreadRows(item)
                }
                else -> {
                    if (outputs.isEmpty()) Text("Wiki 결과를 기다리고 있어요.", style = MaterialTheme.typography.bodySmall)
                    outputs.forEach { page ->
                        PageLink(page, availability, vault, onError)
                    }
                }
            }
        }
    }
}
