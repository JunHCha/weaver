package io.github.junhcha.weaver.widget

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.review.ReviewRuntime
import io.github.junhcha.weaver.ui.ReviewPanel
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

sealed interface WidgetReviewTarget {
    data class Ready(val id: String, val clickedRev: Long, val latestRev: Long) : WidgetReviewTarget
    data class Unavailable(val message: String) : WidgetReviewTarget
    companion object {
        fun resolve(id: String?, rev: Long, latest: (String) -> Long): WidgetReviewTarget {
            if (id == null || !Regex("[A-Za-z0-9_-]+").matches(id) || rev <= 0)
                return Unavailable("올바른 문서 링크가 아닙니다")
            return try {
                val current = latest(id)
                if (current < rev) Unavailable("최신 revision의 동기화를 기다리세요") else Ready(id, rev, current)
            } catch (error: Exception) {
                Unavailable(if (error.message == "문서가 없습니다") "문서가 사라졌습니다. 목록을 다시 확인하세요."
                    else "문서를 확인할 수 없습니다. 동기화·폴더 접근을 확인하세요.")
            }
        }
    }
}

/** Explicit PendingIntent destination. No approval happens here; ReviewPanel revalidates on submit. */
class WidgetReviewActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val id = intent.getStringExtra(PACKAGE_ID)
        val rev = intent.getLongExtra(REVISION, 0)
        setContent {
            var target by remember { mutableStateOf<WidgetReviewTarget?>(null) }
            LaunchedEffect(id, rev) {
                target = withContext(Dispatchers.IO) {
                    WidgetReviewTarget.resolve(id, rev) { ReviewRuntime.open(applicationContext, it).revision }
                }
            }
            MaterialTheme {
                Surface(Modifier.fillMaxSize()) {
                    Column(Modifier.safeDrawingPadding().padding(16.dp).verticalScroll(rememberScrollState())) {
                        when (val result = target) {
                            null -> Text("최신 제안을 확인하고 있습니다")
                            is WidgetReviewTarget.Unavailable -> Text(result.message)
                            is WidgetReviewTarget.Ready -> {
                                if (result.latestRev != result.clickedRev)
                                    Text("위젯의 r${result.clickedRev} 대신 최신 r${result.latestRev}를 엽니다")
                                ReviewPanel(result.id, onClose = { finish() })
                            }
                        }
                        OutlinedButton(onClick = { finish() }) { Text("닫기") }
                    }
                }
            }
        }
    }
    companion object {
        const val PACKAGE_ID = "package_id"
        const val REVISION = "revision"
        fun intent(context: Context, id: String, rev: Long) = Intent(context, WidgetReviewActivity::class.java)
            // Distinct identity prevents PendingIntent extras leaking between rows or revs.
            .setData(Uri.Builder().scheme("weaver").authority("review").appendPath(id).appendPath(rev.toString()).build())
            .putExtra(PACKAGE_ID, id).putExtra(REVISION, rev)
    }
}
