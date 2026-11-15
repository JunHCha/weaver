package io.github.junhcha.weaver.ui

import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.compositeOver
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/** Weaver deliberately supports a single light palette, including widget review activities. */
internal val weaverColorScheme = lightColorScheme(
    primary = WeaverTokens.indigo, onPrimary = WeaverTokens.surface,
    primaryContainer = WeaverTokens.selected.compositeOver(WeaverTokens.surface), onPrimaryContainer = WeaverTokens.indigo,
    inversePrimary = WeaverTokens.surface,
    secondary = WeaverTokens.muted, onSecondary = WeaverTokens.surface,
    secondaryContainer = WeaverTokens.bg, onSecondaryContainer = WeaverTokens.ink,
    tertiary = WeaverTokens.schedule.color, onTertiary = WeaverTokens.surface,
    tertiaryContainer = kindBackground(WeaverTokens.schedule), onTertiaryContainer = WeaverTokens.ink,
    background = WeaverTokens.bg, onBackground = WeaverTokens.ink,
    surface = WeaverTokens.surface, onSurface = WeaverTokens.ink,
    surfaceVariant = WeaverTokens.bg, onSurfaceVariant = WeaverTokens.muted,
    surfaceTint = WeaverTokens.indigo,
    inverseSurface = WeaverTokens.ink, inverseOnSurface = WeaverTokens.surface,
    error = WeaverTokens.thread, onError = WeaverTokens.surface,
    errorContainer = WeaverTokens.thread.copy(alpha = WeaverTokens.request.tintPercent / 100).compositeOver(WeaverTokens.surface),
    onErrorContainer = WeaverTokens.ink,
    outline = WeaverTokens.muted, outlineVariant = WeaverTokens.line, scrim = WeaverTokens.ink,
    surfaceBright = WeaverTokens.surface, surfaceDim = WeaverTokens.bg,
    surfaceContainerLowest = WeaverTokens.surface, surfaceContainerLow = WeaverTokens.surface,
    surfaceContainer = WeaverTokens.surface, surfaceContainerHigh = WeaverTokens.bg,
    surfaceContainerHighest = WeaverTokens.bg,
)

private fun tokenText(size: String, weight: String = "regular") = TextStyle(
    fontFamily = FontFamily.SansSerif,
    fontSize = WeaverTokens.number("typography.size.$size").sp,
    lineHeight = (WeaverTokens.number("typography.size.$size") * WeaverTokens.number("typography.lineHeight.body")).sp,
    fontWeight = FontWeight(WeaverTokens.number("typography.weight.$weight").toInt()),
    fontFeatureSettings = "tnum",
)

private val weaverTypography = Typography(
    displayLarge = tokenText("page"), displayMedium = tokenText("page"), displaySmall = tokenText("page"),
    headlineLarge = tokenText("page", "semibold"), headlineMedium = tokenText("page", "semibold"),
    headlineSmall = tokenText("section", "semibold"),
    titleLarge = tokenText("section", "semibold"), titleMedium = tokenText("body", "medium"),
    titleSmall = tokenText("dense", "medium"),
    bodyLarge = tokenText("body"), bodyMedium = tokenText("dense"), bodySmall = tokenText("small"),
    labelLarge = tokenText("dense", "medium"), labelMedium = tokenText("small", "medium"),
    labelSmall = tokenText("tiny", "medium"),
)

@Composable
fun WeaverTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = weaverColorScheme,
        typography = weaverTypography,
        shapes = Shapes(
            extraSmall = RoundedCornerShape(WeaverTokens.number("radius.chip").dp),
            small = RoundedCornerShape(WeaverTokens.number("radius.control").dp),
            medium = RoundedCornerShape(WeaverTokens.number("radius.card").dp),
            large = RoundedCornerShape(WeaverTokens.number("radius.card").dp),
            extraLarge = RoundedCornerShape(WeaverTokens.number("radius.card").dp),
        ),
        content = content,
    )
}
