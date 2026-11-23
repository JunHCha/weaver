package io.github.junhcha.weaver.ui

import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.LocalActivity
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.saveable.rememberSaveableStateHolder
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.LocalLifecycleOwner
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.phone.PhoneReadModel
import io.github.junhcha.weaver.pipeline.PipelineViewModel
import java.time.Instant
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

@Composable
fun HomeScreen(
    readModel: PhoneReadModel,
    showPendingCalls: Boolean = false,
    showInbox: Boolean = false,
    onNotificationHandled: () -> Unit = {},
) {
    val activity = LocalActivity.current as ComponentActivity
    val model = remember(activity) {
        ViewModelProvider(
            activity,
            ViewModelProvider.AndroidViewModelFactory(activity.application),
        )[PipelineViewModel::class.java]
    }
    val state by model.state.collectAsState()
    val appContext = LocalContext.current.applicationContext
    // Tag edits go to the paired server; without a pairing the timeline stays read-only.
    val tagHost by produceState<TagEditHost?>(null, appContext, readModel, state) {
        value = withContext(Dispatchers.IO) {
            try {
                io.github.junhcha.weaver.transport.TransportRuntime.api(appContext)?.let {
                    ApiTagEditHost(io.github.junhcha.weaver.phone.TagEditor(it))
                }
            } catch (_: Exception) {
                currentCoroutineContext().ensureActive()
                null
            }
        }
    }
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner, activity) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_STOP && !activity.isChangingConfigurations)
                model.endVisit()
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }
    // Destinations are review, tools and the phone shell; the shell's state is saved while another one replaces it.
    val destinations = rememberSaveableStateHolder()
    var review by rememberSaveable { mutableStateOf<String?>(null) }
    var submitted by rememberSaveable { mutableStateOf(false) }
    var tools by rememberSaveable { mutableStateOf(false) }
    // A new epoch gives the shell a fresh saved-state key, so an inbox tap lands on 지금 scrolled to the top.
    var phoneEpoch by rememberSaveable { mutableIntStateOf(0) }
    var pendingCalls by rememberSaveable { mutableStateOf(false) }
    // Drops the draft with the selection; leaving the review through a fold change keeps it (see LocalReviewStateHolder).
    fun closeReview() {
        review?.let { destinations.removeState("review-$it") }
        review = null
        submitted = false
    }
    // Set during composition, before entering child screens that might fail.
    io.github.junhcha.weaver.diagnostics.CrashDiagnostics.screen =
        io.github.junhcha.weaver.diagnostics.CrashDiagnostics.screen.copy(map_open = false, selected_node = null, settings_open = tools)

    // Notification taps are one-shot: apply them, then tell the activity to drop its flags so recreation or
    // back navigation never replays them. An inbox tap leaves settings and lands on the 지금 tab's approvals.
    LaunchedEffect(showPendingCalls, showInbox) {
        if (showPendingCalls) { tools = true; pendingCalls = true }
        if (showInbox) {
            tools = false
            phoneEpoch++
        }
        if (showPendingCalls || showInbox) onNotificationHandled()
    }
    // The previous shell has left composition by now, so its saved bundle can be dropped.
    LaunchedEffect(phoneEpoch) { if (phoneEpoch > 0) destinations.removeState("phone-${phoneEpoch - 1}") }
    val changes by InboxRuntime.changes.collectAsState()
    LaunchedEffect(changes) { model.refresh() }
    LifecycleResumeEffect(Unit) {
        model.refresh()
        onPauseOrDispose {}
    }
    // The queued verdict survives recreation; restart only the bounded refresh wait after rotation.
    LaunchedEffect(review, submitted) {
        val id = review
        if (id != null && submitted) {
            model.refresh()
            withTimeoutOrNull(3_000) {
                model.state.first { current ->
                    current.error != null || (current.loaded && current.items.none { it.packageId == id && it.needsReview })
                }
            }
            closeReview()
        }
    }
    WeaverTheme {
        Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
            CompositionLocalProvider(LocalReviewStateHolder provides destinations) {
                BoxWithConstraints(Modifier.fillMaxSize().safeDrawingPadding()) {
                    // Wide layouts show the selected review inside the 지금 tab; narrow ones give it the whole screen.
                    val wide = isWide(maxWidth, maxHeight)
                    when {
                        review != null && !wide -> ReviewScreen(
                            checkNotNull(review),
                            onBack = ::closeReview,
                            onSubmitted = { submitted = true },
                        )
                        tools -> {
                            val closeTools = { tools = false; pendingCalls = false }
                            BackHandler(onBack = closeTools)
                            Column(Modifier.fillMaxSize().safeDrawingPadding()) {
                                TextButton(onClick = closeTools, Modifier.defaultMinSize(minHeight = 48.dp)) { Text("‹ 지금") }
                                ToolsScreen(pendingCalls)
                            }
                        }
                        else -> destinations.SaveableStateProvider("phone-$phoneEpoch") {
                            Box(Modifier.fillMaxSize()) {
                                PhoneShell(
                                    readModel,
                                    state.items.statusCounts(),
                                    { Instant.now() },
                                    { tools = true },
                                    { if (it != review) closeReview(); review = it; submitted = false },
                                    selectedReview = review,
                                    onCloseReview = ::closeReview,
                                    onReviewSubmitted = { submitted = true },
                                    tagHost = tagHost,
                                    counterparts = state.items.mapNotNull { item -> item.packageId?.let { id -> item.server?.counterpart?.let { id to it } } }.toMap(),
                                )
                            }
                        }
                    }
                }
            }
        }
    }
}
