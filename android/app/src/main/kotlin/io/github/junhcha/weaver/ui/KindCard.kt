package io.github.junhcha.weaver.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AssistChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.TextButton
import io.github.junhcha.weaver.phone.LinkTarget
import io.github.junhcha.weaver.phone.PhoneSnapshot
import io.github.junhcha.weaver.phone.TagKind
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
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


/** A target must exist in the phone's read model; map metadata alone never invents a directory. */
internal fun fabricSubjectTarget(snapshot: PhoneSnapshot?, name: String, kind: String?): LinkTarget {
    val tag = snapshot?.tags?.firstOrNull { it.name == name } ?: return LinkTarget.None
    return when {
        kind == "person" && tag.kind?.isParty == true -> LinkTarget.Person(name)
        kind == "area" && tag.kind == TagKind.AREA -> LinkTarget.Area(name)
        else -> LinkTarget.None
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
internal fun FabricCardInfo(
    item: PipelineItem,
    subjectTarget: (String, String?) -> LinkTarget = { _, _ -> LinkTarget.None },
    onOpenSubject: (LinkTarget) -> Unit = {},
) {
    val summary = item.summary.trim()
    if (summary.isNotEmpty() && summary != sourceTitle(item.title)) {
        Text(summary, style = MaterialTheme.typography.bodyMedium, maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
    val counterpart = item.counterpart
    if (counterpart == null) Text("상대 미상", style = MaterialTheme.typography.bodySmall)
    else {
        val target = subjectTarget(counterpart, "person")
        TextButton(onClick = { onOpenSubject(target) }, enabled = target != LinkTarget.None) {
            Text("상대 $counterpart", maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
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
    Text((listOf("출처 $source") + hosts).joinToString(" · "), style = MaterialTheme.typography.bodySmall)
    if (item.relatedTags.isNotEmpty()) {
        FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            item.relatedTags.forEach { tag ->
                val target = subjectTarget(tag.name, tag.kind)
                AssistChip(
                    onClick = { onOpenSubject(target) }, enabled = target != LinkTarget.None,
                    label = { Text("#${tag.name}", maxLines = 1, overflow = TextOverflow.Ellipsis) },
                    modifier = Modifier.widthIn(max = 260.dp),
                )
            }
        }
    }
}
