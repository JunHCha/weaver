package io.github.junhcha.weaver.ui

import android.animation.ValueAnimator
import android.graphics.Paint
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.LocalActivity
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.VectorConverter
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.rememberTransformableState
import androidx.compose.foundation.gestures.transformable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.saveable.rememberSaveableStateHolder
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.drawIntoCanvas
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Popup
import androidx.compose.ui.window.PopupProperties
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.LocalLifecycleOwner
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.pipeline.*
import io.github.junhcha.weaver.schema.*
import kotlin.math.roundToInt
import kotlinx.coroutines.delay

@Composable
fun HomeScreen(showPendingCalls: Boolean = false, showInbox: Boolean = false) {
    val activity = LocalActivity.current as ComponentActivity
    val model = remember(activity) {
        ViewModelProvider(
            activity,
            ViewModelProvider.AndroidViewModelFactory(activity.application),
        )[PipelineViewModel::class.java]
    }
    val state by model.state.collectAsState()
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner, activity) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_STOP && !activity.isChangingConfigurations)
                model.endVisit()
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }
    var tools by rememberSaveable { mutableStateOf(false) }
    var selected by rememberSaveable { mutableStateOf<String?>(null) }
    var replay by rememberSaveable { mutableIntStateOf(0) }
    LaunchedEffect(showPendingCalls, showInbox) {
        if (showPendingCalls) tools = true
        if (showInbox) selected = MapNode.USER.name
    }
    val changes by InboxRuntime.changes.collectAsState()
    LaunchedEffect(changes) { model.refresh() }
    LifecycleResumeEffect(Unit) {
        model.refresh()
        onPauseOrDispose {}
    }
    MaterialTheme {
        Surface(Modifier.fillMaxSize()) {
            if (tools) {
                BackHandler { tools = false }
                Column(Modifier.safeDrawingPadding()) {
                    TextButton(onClick = { tools = false }) { Text("‹ 데이터 흐름") }
                    ToolsScreen(showPendingCalls, showInbox)
                }
            } else {
                PipelineContent(
                    state,
                    selected?.let(MapNode::valueOf),
                    { selected = it?.name },
                    replay,
                    { replay++ },
                    { tools = true },
                    model::claimAnimation,
                )
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun PipelineContent(
    state: PipelineScreenState,
    selected: MapNode?,
    onSelect: (MapNode?) -> Unit,
    replay: Int,
    onReplay: () -> Unit,
    onTools: () -> Unit,
    claimAnimation: (String) -> Boolean = { true },
) {
    var bubble by remember { mutableStateOf<MapItem?>(null) }
    var focusedItem by rememberSaveable { mutableStateOf<String?>(null) }
    val panelState = rememberSaveableStateHolder()
    Column(Modifier.fillMaxSize().safeDrawingPadding()) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 8.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column {
                Text("내 데이터 흐름", style = MaterialTheme.typography.titleLarge)
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        "지난번 이후 ${state.paths.size}건 이동 ·",
                        style = MaterialTheme.typography.labelMedium,
                    )
                    TextButton(onClick = onReplay) { Text("다시 보기") }
                }
            }
            TextButton(onClick = onTools) { Text("수집·설정") }
        }
        state.error?.let {
            Text(it, Modifier.padding(horizontal = 20.dp), color = MaterialTheme.colorScheme.error)
        }
        BoxWithConstraints(Modifier.fillMaxSize()) {
            val wide = maxWidth >= 840.dp && maxWidth > maxHeight
            Row(Modifier.fillMaxSize()) {
                Column(
                    Modifier.weight(1f).fillMaxHeight(),
                    verticalArrangement = Arrangement.Center,
                ) {
                    Box(Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center) {
                        PipelineMap(
                            state.items,
                            state.paths,
                            replay + state.visitId * 1000000,
                            selected,
                            { focusedItem = null; onSelect(it) },
                            { item ->
                                if (item.server?.wikiEdit != null) {
                                    focusedItem = item.id
                                    onSelect(item.node)
                                } else bubble = item
                            },
                            claimAnimation,
                        )
                    }
                    Legend()
                    Text(
                        if (state.loaded)
                            "전체 ${state.items.size}건 · 할 일 ${state.items.count { it.needsReview }} · 멈춤 ${state.items.count { it.stopped }}"
                        else "데이터를 읽고 있어요",
                        style = MaterialTheme.typography.labelSmall,
                        modifier = Modifier.padding(16.dp),
                    )
                    if (state.loaded && state.items.isEmpty())
                        Text(
                            "아직 수집한 데이터가 없어요. 수집·설정에서 시작해 주세요.",
                            style = MaterialTheme.typography.bodySmall,
                            modifier = Modifier.padding(horizontal = 16.dp),
                        )
                }
                if (wide && selected != null) {
                    VerticalDivider()
                    panelState.SaveableStateProvider(selected.name) {
                        NodePanel(
                            selected,
                            state.items,
                            { onSelect(null) },
                            Modifier.width(340.dp).fillMaxHeight(),
                            focusedItemId = focusedItem,
                        )
                    }
                }
            }
            if (!wide && selected != null)
                ModalBottomSheet(onDismissRequest = { onSelect(null) }) {
                    panelState.SaveableStateProvider(selected.name) {
                        NodePanel(
                            selected,
                            state.items,
                            { onSelect(null) },
                            Modifier.fillMaxWidth().fillMaxHeight(.84f),
                            focusedItemId = focusedItem,
                        )
                    }
                }
        }
    }
    if (selected != null) BackHandler { onSelect(null) }
    bubble?.let { item ->
        Popup(
            alignment = Alignment.Center,
            onDismissRequest = { bubble = null },
            properties = PopupProperties(focusable = true),
        ) {
            Surface(
                shape = RoundedCornerShape(12.dp),
                tonalElevation = 6.dp,
                shadowElevation = 8.dp,
                color = MaterialTheme.colorScheme.inverseSurface,
                contentColor = MaterialTheme.colorScheme.inverseOnSurface,
            ) {
                Column(Modifier.widthIn(max = 300.dp).clickable { bubble = null }.padding(18.dp)) {
                    Text(item.title, style = MaterialTheme.typography.titleSmall)
                    Text(item.stateLabel, style = MaterialTheme.typography.bodySmall)
                    Text("눌러서 닫기", style = MaterialTheme.typography.labelSmall)
                }
            }
        }
    }
}

private val centers =
    mapOf(
        MapNode.SHEPHERD to Offset(130f, 120f),
        MapNode.SPINNER to Offset(440f, 120f),
        MapNode.FABRIC to Offset(750f, 120f),
        MapNode.USER to Offset(130f, 355f),
        MapNode.LOOM to Offset(440f, 355f),
        MapNode.WIKI to Offset(750f, 355f),
        MapNode.COURIER to Offset(252f, 120f),
    )
private val ink = Color(0xff5f5bb6)
private val line = Color(0xffaab6d6)
internal val wikiEditColor = Color(0xff0a8278)

private fun sourceColor(source: String) =
    when (source) {
        "Wiki 직접 수정" -> wikiEditColor
        "통화" -> Color(0xff5b96d6)
        "음성 메모" -> Color(0xff56ad98)
        else -> Color(0xffd9af67)
    }

private fun destination(item: MapItem, items: List<MapItem>): Offset {
    if (item.node == MapNode.COURIER) {
        val i = items.filter { it.node == item.node }.indexOf(item)
        return Offset(if (i < 6) 180f + i * 15f else 315f + (i - 6) * 15f, 120f)
    }
    val same = items.filter { it.node == item.node }
    val i = same.indexOf(item)
    return centers.getValue(item.node) +
        Offset((i % 6 - (minOf(same.size, 6) - 1) / 2f) * 18f, 95f + (i / 6) * 18f)
}

private fun routePoints(path: List<MapNode>): List<Offset> = buildList {
    path.forEachIndexed { i, node ->
        val previous = path.getOrNull(i - 1)
        if (previous == MapNode.LOOM && node == MapNode.USER)
            addAll(listOf(Offset(400f, 337f), Offset(285f, 337f), Offset(170f, 337f)))
        if (previous == MapNode.USER && node == MapNode.LOOM)
            addAll(listOf(Offset(170f, 373f), Offset(285f, 373f), Offset(400f, 373f)))
        if (previous == MapNode.LOOM && node == MapNode.FABRIC)
            addAll(listOf(Offset(590f, 355f), Offset(590f, 120f)))
        if (previous == MapNode.WIKI && node == MapNode.FABRIC)
            addAll(listOf(Offset(925f, 355f), Offset(925f, 120f)))
        add(centers.getValue(node))
    }
}

@Composable
private fun PipelineMap(
    items: List<MapItem>,
    paths: Map<String, List<MapNode>>,
    replay: Int,
    selected: MapNode?,
    onSelect: (MapNode) -> Unit,
    onBubble: (MapItem) -> Unit,
    claimAnimation: (String) -> Boolean,
) {
    var zoom by remember { mutableFloatStateOf(1f) }
    var pan by remember { mutableStateOf(Offset.Zero) }
    val transform = rememberTransformableState { z, p, _ ->
        zoom = (zoom * z).coerceIn(1f, 3f)
        pan = if (zoom == 1f) Offset.Zero else pan + p
    }
    Box(Modifier.aspectRatio(1000f / 490f).clipToBounds().transformable(transform)) {
        BoxWithConstraints(
            Modifier.fillMaxSize().graphicsLayer {
                scaleX = zoom
                scaleY = zoom
                translationX = pan.x
                translationY = pan.y
            }
        ) {
            val mapWidth = maxWidth
            val density = LocalDensity.current
            val pixelScale = with(density) { mapWidth.toPx() } / 1000f
            Canvas(
                Modifier.fillMaxSize().semantics {
                    contentDescription = "폰, Mac, Obsidian vault의 3열 2행 데이터 흐름 지도"
                }
            ) {
                val k = size.width / 1000f
                fun region(path: Path) {
                    drawPath(path, Color.White)
                    drawPath(
                        path,
                        Color(0xffcbd3e0),
                        style =
                            Stroke(
                                1.2f * k,
                                pathEffect = PathEffect.dashPathEffect(floatArrayOf(5 * k, 5 * k)),
                            ),
                    )
                }
                fun rectPath(x: Float, y: Float, w: Float, h: Float) =
                    Path().apply {
                        addRoundRect(
                            androidx.compose.ui.geometry.RoundRect(
                                x * k,
                                y * k,
                                (x + w) * k,
                                (y + h) * k,
                                CornerRadius(18 * k),
                            )
                        )
                    }
                region(rectPath(20f, 30f, 220f, 430f))
                region(
                    Path().apply {
                        moveTo(350 * k, 30 * k)
                        lineTo(880 * k, 30 * k)
                        lineTo(880 * k, 251 * k)
                        lineTo(550 * k, 251 * k)
                        lineTo(550 * k, 460 * k)
                        lineTo(330 * k, 460 * k)
                        lineTo(330 * k, 30 * k)
                        close()
                    }
                )
                region(rectPath(640f, 270f, 240f, 190f))
                label("폰", 38f, 54f, 12f, k)
                label("Mac", 350f, 54f, 12f, k)
                label("Obsidian vault", 660f, 294f, 12f, k)
                edge(listOf(Offset(170f, 120f), Offset(398f, 120f)), k)
                label("수확한 것", 285f, 82f, 11f, k, true)
                edge(listOf(Offset(440f, 223f), Offset(440f, 307f)), k)
                label("정리된 글", 477f, 267f, 10f, k, true)
                edge(listOf(Offset(400f, 337f), Offset(172f, 337f)), k)
                label("엮은 내용", 285f, 320f, 11f, k, true)
                edge(listOf(Offset(170f, 373f), Offset(398f, 373f)), k)
                label("승인", 285f, 404f, 11f, k, true)
                edge(
                    listOf(
                        Offset(480f, 355f),
                        Offset(590f, 355f),
                        Offset(590f, 120f),
                        Offset(708f, 120f),
                    ),
                    k,
                )
                label("Thread 기록", 590f, 237f, 10f, k, true)
                edge(listOf(Offset(750f, 223f), Offset(750f, 307f)), k)
                label("Wiki 갱신", 787f, 267f, 10f, k, true)
                edge(listOf(Offset(790f, 355f), Offset(925f, 355f), Offset(925f, 120f), Offset(790f, 120f)), k, wikiEditColor)
                label("사용자 수정", 925f, 224f, 10f, k, true)
                label("Thread 반영", 925f, 242f, 10f, k, true)
                for ((node, c) in centers) if (node != MapNode.COURIER) {
                    drawRoundRect(
                        if (selected == node) Color(0xfff4f2ff) else Color.White,
                        Offset((c.x - 38) * k, (c.y - 38) * k),
                        Size(76 * k, 76 * k),
                        CornerRadius(21 * k),
                    )
                    drawRoundRect(
                        if (node == MapNode.USER) Color(0xffd99430)
                        else if (selected == node) ink else Color(0xffdbe1ec),
                        Offset((c.x - 38) * k, (c.y - 38) * k),
                        Size(76 * k, 76 * k),
                        CornerRadius(21 * k),
                        style = Stroke(1.5f * k),
                    )
                    label(node.label, c.x, c.y + 60, 16f, k, true, true)
                    label(node.subtitle, c.x, c.y + 78, 10f, k, true)
                }
                centers
                    .filterKeys { it != MapNode.COURIER }
                    .forEach { (node, c) ->
                        val overflow = items.count { it.node == node } - 12
                        if (overflow > 0)
                            label("외 ${overflow}건 · 노드에서 전체 보기", c.x, c.y + 130, 9f, k, true)
                    }
                for ((node, count) in
                    listOf(
                        MapNode.USER to items.count { it.needsReview },
                        MapNode.LOOM to items.count { it.stopped },
                    )) {
                    val c = centers.getValue(node)
                    val warn = node == MapNode.USER
                    drawRoundRect(
                        if (warn) Color(0xfffff0d3) else Color(0xfffce6e8),
                        Offset((c.x + 5) * k, (c.y - 50) * k),
                        Size(62 * k, 21 * k),
                        CornerRadius(10 * k),
                    )
                    label(
                        "${if(warn)"할 일" else "멈춤"} $count",
                        c.x + 36,
                        c.y - 35,
                        10f,
                        k,
                        true,
                        true,
                    )
                }
            }
            for ((node, c) in centers) if (node != MapNode.COURIER) {
                Box(
                    Modifier.offset {
                            IntOffset(
                                ((c.x - 38) * pixelScale).roundToInt(),
                                ((c.y - 38) * pixelScale).roundToInt(),
                            )
                        }
                        .size(mapWidth * 76f / 1000f)
                        .clickable { onSelect(node) }
                        .semantics { contentDescription = "${node.label} 노드" },
                    contentAlignment = Alignment.Center,
                ) {
                    Icon(
                        PipelineIcons.get(node),
                        null,
                        Modifier.size(mapWidth * 44f / 1000f),
                        tint = ink,
                    )
                }
            }
            for (y in listOf(120f, 337f, 373f)) Box(
                Modifier.offset {
                        IntOffset(
                            (269 * pixelScale).roundToInt(),
                            ((y - 16) * pixelScale).roundToInt(),
                        )
                    }
                    .size(mapWidth * 32f / 1000f)
                    .background(Color(0xfff0effb), RoundedCornerShape(50)),
                contentAlignment = Alignment.Center,
            ) {
                Icon(
                    PipelineIcons.get(MapNode.COURIER),
                    "Courier",
                    Modifier.size(mapWidth * 22f / 1000f),
                    tint = ink,
                )
            }
            items
                .filter { item -> items.filter { it.node == item.node }.indexOf(item) < 12 }
                .forEachIndexed { index, item ->
                    key(item.id) {
                        val final = destination(item, items)
                        val position =
                            remember(item.id) { Animatable(final, Offset.VectorConverter) }
                        LaunchedEffect(replay, item.node, final) {
                            val path = paths[item.id].orEmpty()
                            if (
                                path.size > 1 &&
                                    claimAnimation("$replay:${item.id}") &&
                                    ValueAnimator.areAnimatorsEnabled()
                            ) {
                                val points = routePoints(path) + final
                                position.snapTo(points.first())
                                delay(180L + index * 120L)
                                points.drop(1).forEach { position.animateTo(it, tween(200)) }
                            } else position.snapTo(final)
                        }
                        Box(
                            Modifier.offset {
                                    IntOffset(
                                        (position.value.x * pixelScale -
                                                with(density) { 16.dp.toPx() })
                                            .roundToInt(),
                                        (position.value.y * pixelScale -
                                                with(density) { 16.dp.toPx() })
                                            .roundToInt(),
                                    )
                                }
                                .size(32.dp)
                                .clickable { onBubble(item) }
                                .semantics {
                                    contentDescription = "${item.title}: ${item.stateLabel}"
                                }
                        ) {
                            Canvas(Modifier.fillMaxSize()) {
                                val s = 12f * pixelScale
                                val top = Offset((size.width - s) / 2, (size.height - s) / 2)
                                drawRoundRect(
                                    sourceColor(item.source),
                                    top,
                                    Size(s, s),
                                    CornerRadius(3 * pixelScale),
                                )
                                drawRoundRect(
                                    if (item.needsReview) Color(0xffd78a26)
                                    else if (item.stopped) Color(0xffd34e59) else Color.White,
                                    top,
                                    Size(s, s),
                                    CornerRadius(3 * pixelScale),
                                    style =
                                        Stroke(
                                            if (item.needsReview || item.stopped) 2.5f * pixelScale
                                            else pixelScale
                                        ),
                                )
                            }
                        }
                    }
                }
        }
    }
}

private fun DrawScope.label(
    text: String,
    x: Float,
    y: Float,
    font: Float,
    k: Float,
    center: Boolean = false,
    bold: Boolean = false,
) {
    drawIntoCanvas {
        it.nativeCanvas.drawText(
            text,
            x * k,
            y * k,
            Paint(Paint.ANTI_ALIAS_FLAG).apply {
                color = android.graphics.Color.rgb(100, 113, 133)
                textSize = font * k
                textAlign = if (center) Paint.Align.CENTER else Paint.Align.LEFT
                isFakeBoldText = bold
            },
        )
    }
}

private fun DrawScope.edge(points: List<Offset>, k: Float, color: Color = line) {
    points.zipWithNext().forEach { (a, b) -> drawLine(color, a * k, b * k, 2 * k) }
    val end = points.last()
    val d = (end - points[points.lastIndex - 1])
    val unit = d / d.getDistance()
    val side = Offset(-unit.y, unit.x)
    drawPath(
        Path().apply {
            moveTo(end.x * k, end.y * k)
            val a = end - unit * 9f + side * 4f
            val b = end - unit * 9f - side * 4f
            lineTo(a.x * k, a.y * k)
            lineTo(b.x * k, b.y * k)
            close()
        },
        color,
    )
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun Legend() {
    FlowRow(
        Modifier.fillMaxWidth().padding(horizontal = 12.dp),
        horizontalArrangement = Arrangement.spacedBy(12.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        for (source in listOf("통화", "음성 메모", "공유한 글", "Wiki 직접 수정")) Row(
            verticalAlignment = Alignment.CenterVertically
        ) {
            Box(Modifier.size(8.dp).background(sourceColor(source), RoundedCornerShape(2.dp)))
            Spacer(Modifier.width(3.dp))
            Text(source, style = MaterialTheme.typography.labelSmall)
        }
        Text("주황: 내 차례 · 빨강: 멈춤", style = MaterialTheme.typography.labelSmall)
    }
}
