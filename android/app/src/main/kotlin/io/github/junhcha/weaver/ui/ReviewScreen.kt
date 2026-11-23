package io.github.junhcha.weaver.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.SaveableStateHolder
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp

/**
 * Holder that keeps a review's unsent input outside the screen that happens to show it, so a fold change can move the
 * review between the inline pane and the full screen without losing it. Null (tests, previews) keeps plain state.
 */
internal val LocalReviewStateHolder = staticCompositionLocalOf<SaveableStateHolder?> { null }

/** Gives [content] the review's saved input regardless of where it sits in the tree; see [LocalReviewStateHolder]. */
@Composable
internal fun KeepReviewState(id: String, content: @Composable () -> Unit) {
    val holder = LocalReviewStateHolder.current
    if (holder == null) content() else holder.SaveableStateProvider("review-$id", content)
}

/** The wide layout's detail pane: the review inline, with the source beside it. */
@Composable
internal fun ReviewPane(id: String, onClose: () -> Unit, onSubmitted: () -> Unit) {
    var busy by remember(id) { mutableStateOf(false) }
    BackHandler { if (!busy) onClose() }
    Column(Modifier.fillMaxSize().testTag("review-screen")) {
        KeepReviewState(id) {
            ReviewPanel(id, onSubmitted = onSubmitted, onBusyChanged = { busy = it }, wide = true, onClose = { if (!busy) onClose() })
        }
    }
}

/** A full-screen destination above the phone shell, with both back actions returning to its saved origin. */
@Composable
internal fun ReviewScreen(id: String, onBack: () -> Unit, onSubmitted: () -> Unit) {
    var busy by remember(id) { mutableStateOf(false) }
    // Consume system back while submission owns the IO operation; popping would cancel its scope.
    BackHandler { if (!busy) onBack() }
    Column(Modifier.fillMaxSize().safeDrawingPadding().testTag("review-screen")) {
        TextButton(onClick = onBack, enabled = !busy, modifier = Modifier.defaultMinSize(minHeight = 48.dp)) {
            Text("‹ 지금")
        }
        HorizontalDivider()
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(20.dp)) {
            KeepReviewState(id) {
                ReviewPanel(
                    id,
                    onSubmitted = onSubmitted,
                    onBusyChanged = { busy = it },
                    showCloseButton = false,
                    onClose = onBack,
                )
            }
        }
    }
}
