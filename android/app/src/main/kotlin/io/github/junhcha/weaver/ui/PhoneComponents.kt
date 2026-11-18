package io.github.junhcha.weaver.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.graphics.luminance
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
internal fun SectionHeading(text: String, emphasis: Boolean = false) =
    Text(
        text, Modifier.padding(top = 12.dp, bottom = 4.dp),
        style = MaterialTheme.typography.labelMedium.let { if (emphasis) it.copy(fontWeight = FontWeight.Bold) else it },
        color = if (emphasis) WeaverTokens.indigo else WeaverTokens.muted,
    )

/** Chip like the prototype: outlined, filled with the indigo brand color when on. */
@Composable
internal fun PhoneChip(label: String, selected: Boolean, onClick: (() -> Unit)?, modifier: Modifier = Modifier) {
    val shape = RoundedCornerShape(WeaverTokens.number("radius.pill").dp * 2)
    Surface(
        // Toggles are checkboxes with a state; the fixed 상대 chip is a plain label. 48dp touch target either way.
        modifier = modifier.heightIn(min = 48.dp).let {
            if (onClick != null) it.toggleable(selected, role = Role.Checkbox, onValueChange = { onClick() }) else it
        },
        shape = shape,
        color = if (selected) WeaverTokens.indigo else WeaverTokens.surface,
        contentColor = if (selected) WeaverTokens.surface else WeaverTokens.ink,
        border = BorderStroke(1.dp, if (selected) WeaverTokens.indigo else WeaverTokens.line),
    ) {
        Box(Modifier.padding(horizontal = 12.dp), contentAlignment = Alignment.Center) {
            Text(label, style = MaterialTheme.typography.labelMedium)
        }
    }
}

/** A Thread as a kind-coloured card: type band on the left, content, then a muted detail line. */
@Composable
internal fun ThreadCard(
    thread: PhoneThread,
    detail: String,
    modifier: Modifier = Modifier,
    badge: String? = null,
    muted: Boolean = false,
) {
    KindCard(modifier, kind = thread.kind.styleKey) {
        Column(Modifier.padding(horizontal = 10.dp, vertical = 8.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                // Muted rows use the muted ink (>= 4.5:1 on the card), not alpha, so the text stays readable.
                Text(
                    thread.content, Modifier.weight(1f, fill = false), style = MaterialTheme.typography.bodyMedium,
                    color = if (muted) WeaverTokens.muted else WeaverTokens.ink,
                )
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

/** WCAG contrast ratio of two opaque colours. */
internal fun contrastRatio(a: androidx.compose.ui.graphics.Color, b: androidx.compose.ui.graphics.Color): Double {
    val la = a.luminance().toDouble()
    val lb = b.luminance().toDouble()
    return (maxOf(la, lb) + 0.05) / (minOf(la, lb) + 0.05)
}
