package io.github.junhcha.weaver.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp

/** A full-screen destination above the map, with both back actions returning to its saved origin. */
@Composable
internal fun ReviewScreen(id: String, onBack: () -> Unit, onSubmitted: () -> Unit) {
    BackHandler(onBack = onBack)
    Column(Modifier.fillMaxSize().safeDrawingPadding().testTag("review-screen")) {
        TextButton(onClick = onBack, modifier = Modifier.defaultMinSize(minHeight = 48.dp)) {
            Text("‹ 데이터 흐름")
        }
        HorizontalDivider()
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(20.dp)) {
            ReviewPanel(id, onSubmitted = onSubmitted, showCloseButton = false, onClose = onBack)
        }
    }
}
