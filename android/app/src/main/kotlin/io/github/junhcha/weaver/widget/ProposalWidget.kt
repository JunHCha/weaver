package io.github.junhcha.weaver.widget

import android.content.Context
import android.content.Intent
import androidx.compose.runtime.*
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.glance.*
import androidx.glance.action.clickable
import androidx.glance.appwidget.*
import androidx.glance.appwidget.action.actionStartActivity
import androidx.glance.layout.*
import androidx.glance.text.Text
import androidx.glance.text.TextStyle
import androidx.glance.unit.ColorProvider
import io.github.junhcha.weaver.MainActivity
import io.github.junhcha.weaver.inbox.InboxRuntime
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

class ProposalWidget : GlanceAppWidget() {
    override val sizeMode = SizeMode.Responsive(setOf(SMALL, LARGE))
    override suspend fun provideGlance(context: Context, id: GlanceId) {
        val initial = withContext(Dispatchers.IO) { WidgetModel.load(context) }
        provideContent {
            val change by WidgetUpdates.changes.collectAsState()
            var model by remember { mutableStateOf(initial) }
            LaunchedEffect(change) { model = withContext(Dispatchers.IO) { WidgetModel.load(context) } }
            WidgetContent(context, model, LocalSize.current.height >= LARGE.height)
        }
    }
    companion object {
        val SMALL = DpSize(250.dp, 230.dp)
        val LARGE = DpSize(300.dp, 420.dp)
    }
}

@Composable
internal fun WidgetContent(context: Context, model: WidgetModel, large: Boolean) {
    val normal = TextStyle(color = ColorProvider(Color(0xFF17251E)), fontSize = 14.sp)
    Column(GlanceModifier.fillMaxSize().background(Color(0xFFF0F5EF)).padding(12.dp)) {
        Text(if (model.failed) "Weaver · 목록 확인 필요" else "Weaver · 승인 대기 ${model.pendingCount}건", style = normal, maxLines = 1,
            modifier = GlanceModifier.clickable(actionStartActivity(
                Intent(context, MainActivity::class.java).setAction(InboxRuntime.OPEN_INBOX))))
        if (model.rows.isEmpty() && !model.failed) Text("승인 대기 제안 없음", style = normal, maxLines = 1)
        model.visible(large).forEach { row ->
            Text("${if (row.submitted) row.deliveryStatus ?: "전송됨" else "승인 대기"} · ${row.source} · ${row.summary}",
                style = normal, maxLines = 1,
                modifier = GlanceModifier.fillMaxWidth().padding(vertical = 6.dp)
                    .clickable(actionStartActivity(WidgetReviewActivity.intent(context, row.id, row.rev))))
        }
        if (model.overflow(large) > 0) Text("외 ${model.overflow(large)}건", style = normal, maxLines = 1)
        model.warning(large)?.let { Text(it, style = normal, maxLines = if (large) 3 else 2) }
        Spacer(GlanceModifier.defaultWeight())
        Text(model.status, style = normal, maxLines = 2)
        Text("절전 시 지연될 수 있음 · 앱에서 확인", style = normal, maxLines = 1,
            modifier = GlanceModifier.clickable(actionStartActivity(
                Intent(context, MainActivity::class.java).setAction(InboxRuntime.OPEN_INBOX))))
    }
}

class ProposalWidgetReceiver : GlanceAppWidgetReceiver() {
    override val glanceAppWidget: GlanceAppWidget = ProposalWidget()
}
