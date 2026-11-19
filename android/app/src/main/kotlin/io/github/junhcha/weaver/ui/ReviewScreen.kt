package io.github.junhcha.weaver.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp

/** A full-screen destination above the map, with both back actions returning to its saved origin. */
@Composable
internal fun ReviewScreen(id: String, onBack: () -> Unit, onSubmitted: () -> Unit) {
    var busy by remember(id) { mutableStateOf(false) }
    // Consume system back while submission owns the IO operation; popping would cancel its scope.
    BackHandler { if (!busy) onBack() }
    Column(Modifier.fillMaxSize().safeDrawingPadding().testTag("review-screen")) {
        TextButton(onClick = onBack, enabled = !busy, modifier = Modifier.defaultMinSize(minHeight = 48.dp)) {
            Text("‹ 데이터 흐름")
        }
        HorizontalDivider()
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(20.dp)) {
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
