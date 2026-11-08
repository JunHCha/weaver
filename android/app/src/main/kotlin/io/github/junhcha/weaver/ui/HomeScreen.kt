package io.github.junhcha.weaver.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import io.github.junhcha.weaver.R
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

/** Home screen. Opens with any combination of denied permissions; nothing is asked at launch. */
@Composable
fun HomeScreen(showPendingCalls: Boolean = false, showInbox: Boolean = false) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var granted by remember { mutableStateOf(Feature.entries.associateWith { it.isGranted(context) }) }
    val refresh = {
        granted = Feature.entries.associateWith { it.isGranted(context) }
        scope.launch(Dispatchers.IO) { io.github.junhcha.weaver.shepherd.ShepherdRuntime.schedule(context) }
    }
    LifecycleResumeEffect(Unit) {
        refresh()
        onPauseOrDispose { }
    }
    val launcher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions(),
    ) { refresh() }

    MaterialTheme {
        Surface(modifier = Modifier.fillMaxSize()) {
            Column(
                modifier = Modifier
                    .safeDrawingPadding()
                    .verticalScroll(rememberScrollState())
                    .padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                Text(stringResource(R.string.app_name), style = MaterialTheme.typography.headlineMedium)
                Text(stringResource(R.string.home_subtitle), style = MaterialTheme.typography.bodyMedium)
                if (showInbox) InboxPanel(openedFromNotification = true)
                ShepherdPanel(showPendingCalls)
                if (!showInbox) InboxPanel()
                CourierPanel()
                Feature.entries.forEach { feature ->
                    FeatureCard(
                        feature = feature,
                        granted = granted.getValue(feature),
                        onRequest = {
                            if (feature == Feature.SHEPHERD) {
                                scope.launch(Dispatchers.IO) { io.github.junhcha.weaver.shepherd.ShepherdRuntime.activate(context) }
                            }
                            launcher.launch(feature.permissions().toTypedArray())
                        },
                    )
                }
            }
        }
    }
}

@Composable
private fun FeatureCard(feature: Feature, granted: Boolean, onRequest: () -> Unit) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(stringResource(feature.title), style = MaterialTheme.typography.titleMedium)
            Text(stringResource(feature.description), style = MaterialTheme.typography.bodySmall)
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    stringResource(if (granted) R.string.permission_granted else R.string.permission_denied),
                    style = MaterialTheme.typography.labelLarge,
                )
                if (!granted) {
                    OutlinedButton(onClick = onRequest) { Text(stringResource(R.string.permission_request)) }
                }
            }
        }
    }
}
