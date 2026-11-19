package io.github.junhcha.weaver.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import io.github.junhcha.weaver.phone.LinkTarget
import io.github.junhcha.weaver.phone.PhoneSnapshot
import io.github.junhcha.weaver.phone.TagKind
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.foundation.clickable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.compositeOver
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.text.style.LineBreak
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.schema.PipelineItem
import io.github.junhcha.weaver.schema.PipelineThread
import io.github.junhcha.weaver.schema.Stage

internal fun threadKindStyle(label: String?): WeaverTokens.Kind = when (label) {
    "요청", "request" -> WeaverTokens.request
    "결정", "decision" -> WeaverTokens.decision
    "일정", "schedule" -> WeaverTokens.schedule
    "태그정정", "거절", "실행", "hollow" -> WeaverTokens.hollow
    else -> WeaverTokens.record
}

/** Match web color-mix(in srgb, kind tintPercent, surface). Hollow leaves the parent visible. */
internal fun kindBackground(kind: WeaverTokens.Kind): Color =
    if (kind.dashed) Color.Transparent
    else kind.color.copy(alpha = kind.tintPercent / 100).compositeOver(WeaverTokens.surface)

@Composable
internal fun KindCard(modifier: Modifier = Modifier, kind: String? = null, content: @Composable ColumnScope.() -> Unit) {
    val style = threadKindStyle(kind)
    Surface(
        modifier = modifier.fillMaxWidth(), color = kindBackground(style),
        contentColor = WeaverTokens.ink,
        shape = RoundedCornerShape(WeaverTokens.number("radius.card").dp),
    ) {
        Column(Modifier.drawBehind {
            val width = WeaverTokens.number("layout.kindBand").dp.toPx()
            drawLine(style.color, Offset(width / 2, 0f), Offset(width / 2, size.height), width,
                pathEffect = if (style.dashed) PathEffect.dashPathEffect(floatArrayOf(width * 2, width)) else null)
        }.padding(start = WeaverTokens.number("layout.kindBand").dp), content = content)
    }
}

// Mixed documents keep a neutral record container; every child retains its actual kind.
internal fun List<PipelineThread>.cardKind(): String? = map { it.kind }.distinct().singleOrNull()

@Composable
internal fun ThreadRows(item: PipelineItem, cardTime: String? = null) {
    item.threads.forEach { thread ->
        KindCard(kind = thread.kind) {
            Column(Modifier.padding(8.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(
                    threadTitle(thread.content),
                    style = androidx.compose.material3.MaterialTheme.typography.titleSmall.copy(lineBreak = LineBreak.Heading),
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
                Text(
                    threadMeta(thread.kind, item.stageTimes[Stage.APPLY] ?: item.updatedAt, cardTime),
                    style = androidx.compose.material3.MaterialTheme.typography.labelSmall,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
    }
}


private fun nfc(text: String) = java.text.Normalizer.normalize(text, java.text.Normalizer.Form.NFC)

/**
 * A target must exist in the phone's read model with the same name and kind; map metadata alone
 * never invents a directory. People and organizations share the people tab.
 */
internal fun fabricSubjectTarget(snapshot: PhoneSnapshot?, name: String, kind: String?): LinkTarget {
    val wanted = nfc(name)
    val tags = snapshot?.tags?.filter { nfc(it.name) == wanted }.orEmpty()
    return when (kind) {
        "person", "organization" -> tags.firstOrNull { it.kind?.isParty == true }?.let { LinkTarget.Person(it.name) }
        "area" -> tags.firstOrNull { it.kind == TagKind.AREA }?.let { LinkTarget.Area(it.name) }
        else -> null
    } ?: LinkTarget.None
}

private fun openLabel(target: LinkTarget): String? = when (target) {
    is LinkTarget.Person -> "사람 타임라인 열기"
    is LinkTarget.Area -> "영역 타임라인 열기"
    else -> null
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
internal fun FabricCardInfo(
    item: PipelineItem,
    subjectTarget: (String, String?) -> LinkTarget = { _, _ -> LinkTarget.None },
    onOpenSubject: (LinkTarget) -> Unit = {},
    showCounterpart: Boolean = true,
) {
    val summary = item.summary.trim()
    if (summary.isNotEmpty() && summary != sourceTitle(item.title)) {
        Text(summary, style = MaterialTheme.typography.bodyMedium, maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
    val counterpart = item.counterpart?.takeIf { showCounterpart && it.isNotBlank() }
    if (counterpart != null) {
        val target = subjectTarget(counterpart, "person")
        val label = openLabel(target)
        Text(
            "상대 $counterpart", style = MaterialTheme.typography.bodySmall, maxLines = 1, overflow = TextOverflow.Ellipsis,
            color = if (label != null) MaterialTheme.colorScheme.primary else Color.Unspecified,
            modifier = if (label == null) Modifier else Modifier.defaultMinSize(minHeight = 48.dp)
                .clickable(onClickLabel = label, role = Role.Button) { onOpenSubject(target) }
                .wrapContentHeight(Alignment.CenterVertically),
        )
    }
    val source = when (item.sourceType) {
        "call" -> "통화 녹음"
        "voice_memo" -> "음성 메모"
        "kakao" -> "카카오톡"
        "wiki_edit" -> "Wiki 수정"
        else -> "공유 텍스트"
    }
    val hosts = item.captureHosts.mapNotNull {
        when (it) {
            io.github.junhcha.weaver.schema.CaptureHost.PHONE -> "폰"
            io.github.junhcha.weaver.schema.CaptureHost.MAC -> "Mac"
            io.github.junhcha.weaver.schema.CaptureHost.DEVICE -> "기기"
            else -> null
        }
    }.distinct()
    Text(
        (listOf("출처 $source") + hosts).joinToString(" · "), style = MaterialTheme.typography.bodySmall,
        maxLines = 1, overflow = TextOverflow.Ellipsis,
    )
    if (item.relatedTags.isNotEmpty()) {
        FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            item.relatedTags.forEach { tag ->
                val target = subjectTarget(tag.name, tag.kind)
                val label = openLabel(target)
                val kindLabel = TagKind.fromWire(tag.kind)?.label
                Surface(
                    shape = RoundedCornerShape(8.dp),
                    color = MaterialTheme.colorScheme.surfaceVariant,
                    modifier = Modifier.widthIn(max = 260.dp).defaultMinSize(minHeight = if (label != null) 48.dp else 32.dp)
                        .let { if (label != null) it.clickable(onClickLabel = label, role = Role.Button) { onOpenSubject(target) } else it }
                        .semantics { if (kindLabel != null) contentDescription = "$kindLabel ${tag.name}" },
                ) {
                    Text(
                        "#${tag.name}", style = MaterialTheme.typography.labelLarge, maxLines = 1, overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.padding(horizontal = 12.dp, vertical = 6.dp).wrapContentHeight(Alignment.CenterVertically),
                    )
                }
            }
        }
    }
}
