package io.github.junhcha.weaver.callbrief

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.ui.WeaverTheme

class CallBriefActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        CallBriefWindows.launchPending = false
        setContent {
            WeaverTheme {
                Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
                    Column(Modifier.padding(16.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                        Text(FictionalBrief.title, style = MaterialTheme.typography.headlineSmall)
                        Text("표준 경계 요청 → multi-window=$isInMultiWindowMode")
                        Text(FictionalBrief.body)
                        Button(onClick = { CallBriefWindows.card(this@CallBriefActivity); finishAndRemoveTask() }) { Text("떠 있는 카드로 전환") }
                        TextButton(onClick = { finishAndRemoveTask() }) { Text("닫기 · 통화로 돌아가기") }
                    }
                }
            }
        }
    }

    override fun onPostResume() {
        super.onPostResume()
        // Bounds are a hint. Successful startActivity does not prove freeform support.
        window.decorView.postDelayed({
            if (isFinishing || isDestroyed) return@postDelayed
            if (CallBriefWindows.locked(this)) {
                CallBriefService.notifyBrief(this)
                finishAndRemoveTask()
            } else if (!isInMultiWindowMode) {
                if (!CallBriefWindows.card(this)) CallBriefService.notifyBrief(this)
                finishAndRemoveTask()
            }
        }, 350)
    }
}
