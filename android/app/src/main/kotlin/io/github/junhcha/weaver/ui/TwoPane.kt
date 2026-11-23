package io.github.junhcha.weaver.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/** Unfolded landscape foldables and tablets get two panes; everything else keeps the single-column phone layout. */
fun isWide(maxWidth: Dp, maxHeight: Dp): Boolean = maxWidth >= 840.dp && maxWidth > maxHeight

/**
 * Wide: list (42%) | divider | detail (58%), with [emptyDetail] while nothing is selected.
 * Narrow: the detail fills the screen while selected, the list otherwise. [wide] lets a parent that already measured
 * the same bounds decide, so both always agree.
 */
@Composable
fun TwoPane(list: @Composable () -> Unit, detail: (@Composable () -> Unit)?, emptyDetail: String, wide: Boolean? = null) {
    BoxWithConstraints(Modifier.fillMaxSize()) {
        if (wide ?: isWide(maxWidth, maxHeight)) {
            Row(Modifier.fillMaxSize()) {
                Box(Modifier.weight(0.42f).fillMaxHeight()) { list() }
                Box(Modifier.width(1.dp).fillMaxHeight().background(WeaverTokens.line))
                Box(Modifier.weight(0.58f).fillMaxHeight().testTag("two-pane-detail")) {
                    if (detail != null) detail()
                    else Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { Muted(emptyDetail) }
                }
            }
        } else if (detail != null) detail() else list()
    }
}
