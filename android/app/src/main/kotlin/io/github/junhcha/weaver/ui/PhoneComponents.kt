package io.github.junhcha.weaver.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.phone.PhoneThread
import io.github.junhcha.weaver.phone.SeoulZone
import java.time.Instant
import java.time.LocalDate
import java.time.format.TextStyle
import java.util.Locale

internal fun LocalDate.shortDate(): String = "${monthValue}/${dayOfMonth}"

internal fun Instant.shortDate(): String = atZone(SeoulZone).toLocalDate().shortDate()

internal fun Instant.clock(): String = atZone(SeoulZone).toLocalTime().let { "%02d:%02d".format(it.hour, it.minute) }

internal fun LocalDate.dayOfWeekKo(): String = dayOfWeek.getDisplayName(TextStyle.SHORT, Locale.KOREAN)

/** Small muted label used under card titles and in section headings. */
@Composable
internal fun Muted(text: String, modifier: Modifier = Modifier) =
    Text(text, modifier, style = MaterialTheme.typography.labelMedium, color = WeaverTokens.muted)

@Composable
internal fun SectionHeading(text: String) =
    Text(
        text, Modifier.padding(top = 12.dp, bottom = 4.dp), style = MaterialTheme.typography.labelMedium,
        color = WeaverTokens.muted,
    )

/** Chip like the prototype: outlined, filled with the indigo brand color when on. */
@Composable
internal fun PhoneChip(label: String, selected: Boolean, onClick: (() -> Unit)?, modifier: Modifier = Modifier) {
    val shape = RoundedCornerShape(WeaverTokens.number("radius.pill").dp * 2)
    Surface(
        modifier = modifier.let { if (onClick != null) it.clickable(onClick = onClick) else it },
        shape = shape,
        color = if (selected) WeaverTokens.indigo else WeaverTokens.surface,
        contentColor = if (selected) WeaverTokens.surface else WeaverTokens.ink,
        border = BorderStroke(1.dp, if (selected) WeaverTokens.indigo else WeaverTokens.line),
    ) {
        Text(label, Modifier.padding(horizontal = 12.dp, vertical = 6.dp), style = MaterialTheme.typography.labelMedium)
    }
}

/** A Thread as a kind-coloured card: type band on the left, content, then a muted detail line. */
@Composable
internal fun ThreadCard(
    thread: PhoneThread,
    detail: String,
    modifier: Modifier = Modifier,
    badge: String? = null,
) {
    KindCard(modifier, kind = thread.kind.styleKey) {
        Column(Modifier.padding(horizontal = 10.dp, vertical = 8.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                Text(thread.content, Modifier.weight(1f, fill = false), style = MaterialTheme.typography.bodyMedium)
                if (badge != null) {
                    Box(
                        Modifier.background(WeaverTokens.surface, RoundedCornerShape(WeaverTokens.number("radius.chip").dp))
                            .padding(horizontal = 4.dp),
                    ) { Text(badge, style = MaterialTheme.typography.labelSmall, color = WeaverTokens.muted) }
                }
            }
            if (detail.isNotEmpty()) Muted(detail)
        }
    }
}

internal fun Modifier.dim(dimmed: Boolean): Modifier = if (dimmed) alpha(0.6f) else this

@Composable
internal fun ScreenTitle(title: String, trailing: String? = null, modifier: Modifier = Modifier) {
    Row(
        modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(title, Modifier.weight(1f, fill = false), style = MaterialTheme.typography.titleLarge, color = WeaverTokens.indigo)
        if (trailing != null) {
            Muted(trailing, Modifier.weight(1f).padding(start = 8.dp))
        }
    }
}
