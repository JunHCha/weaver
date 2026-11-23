package io.github.junhcha.weaver.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.compositeOver
import androidx.compose.ui.unit.dp

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
