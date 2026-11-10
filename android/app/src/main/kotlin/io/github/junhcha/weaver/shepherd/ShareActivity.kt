package io.github.junhcha.weaver.shepherd

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.schema.Channel
import io.github.junhcha.weaver.shepherd.autoshare.AutoShareRuntime
import java.io.File
import java.nio.charset.CharacterCodingException
import java.util.concurrent.Executors

class ShareActivity : ComponentActivity() {
    private val io = Executors.newSingleThreadExecutor()
    private var file by mutableStateOf<File?>(null)
    private var preview by mutableStateOf("")
    private var calls by mutableStateOf<List<PendingCall>>(emptyList())
    private var channel by mutableStateOf(Channel.SHARE_TEXT)
    private var callId by mutableStateOf<String?>(null)
    private var busy by mutableStateOf(true)
    private var message by mutableStateOf<String?>(null)
    /** Set when the accessibility job shared this very recording's script; submits without a tap. */
    private var autoCallId: String? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        channel = savedInstanceState?.getString("channel")?.let { runCatching { Channel.valueOf(it) }.getOrNull() }
            ?: Channel.SHARE_TEXT
        callId = savedInstanceState?.getString("callId")
        setContent {
            MaterialTheme {
                Surface(Modifier.fillMaxSize()) {
                    Column(Modifier.systemBarsPadding().padding(24.dp).verticalScroll(rememberScrollState()),
                        verticalArrangement = Arrangement.spacedBy(12.dp)) {
                        Text("공유 텍스트 확인", style = MaterialTheme.typography.headlineSmall)
                        Text("UTF-8 또는 BOM 있는 UTF-16 텍스트 한 항목 · 최대 10 MiB · 종류를 선택하세요.")
                        file?.let { Text("텍스트 ${it.length()} bytes 준비됨") }
                        if (preview.isNotEmpty()) Text(preview)
                        listOf(Channel.SHARE_TEXT to "일반 텍스트", Channel.KAKAO to "카카오톡 내보내기",
                            Channel.CALL to "통화 스크립트").forEach { (kind, label) ->
                            Row {
                                RadioButton(modifier = Modifier.testTag("channel-${kind.name}"), selected = channel == kind, enabled = !busy && file != null &&
                                    (kind != Channel.CALL || calls.isNotEmpty()), onClick = { channel = kind })
                                Text(label, modifier = Modifier.padding(top = 12.dp))
                            }
                        }
                        if (calls.isEmpty()) Text("미처리 통화가 없어 통화 스크립트를 선택할 수 없습니다. 저장되지 않습니다.")
                        if (channel == Channel.CALL) {
                            Text("연결할 통화 (파일명과 일치하는 통화, 없으면 가장 최근 통화가 기본)")
                            calls.forEach { call ->
                                TextButton(enabled = !busy, onClick = { callId = call.id }) {
                                    Text("${if (callId == call.id) "● " else "○ "}${call.occurredAt ?: "시각 미상"} · ${call.displayName} · ${call.counterpart ?: "알 수 없음"}")
                                }
                            }
                        }
                        message?.let { Text(it) }
                        if (busy) CircularProgressIndicator()
                        Button(enabled = !busy && file != null && (channel != Channel.CALL || callId != null),
                            onClick = { submit() }) { Text("Courier로 보내기") }
                        TextButton(enabled = !busy, onClick = { file?.delete(); finish() }) { Text("닫기") }
                    }
                }
            }
        }
        io.execute {
            try {
                val restored = savedInstanceState?.getString("file")?.let(::File)?.takeIf { it.isFile }
                val received = restored ?: SharedText.receive(this, intent)
                val sample = received.reader(Charsets.UTF_8).use { reader ->
                    val chars = CharArray(2000)
                    val count = reader.read(chars)
                    if (count > 0) String(chars, 0, count) else ""
                }
                // Include recordings not yet seen by the periodic worker, even while stabilizing.
                ShepherdRuntime.scanNow(this)
                val pending = PendingCalls.repository(this).pending()
                val displayName = SharedText.displayName(this, intent)
                runOnUiThread {
                    if (isDestroyed) {
                        if (restored == null) received.delete()
                        return@runOnUiThread
                    }
                    preview = sample
                    file = received
                    calls = pending
                    if (savedInstanceState == null) channel = SharedCallSelection.channel(displayName, pending)
                    if (callId !in pending.map { it.id }) callId = SharedCallSelection.call(displayName, pending)
                    busy = false
                    val auto = if (savedInstanceState == null) AutoShareRuntime.autoSubmitCallId(displayName, pending) else null
                    if (auto != null) {
                        autoCallId = auto
                        channel = Channel.CALL
                        callId = auto
                        submit()
                    }
                }
            } catch (error: Exception) { showError(error) }
        }
    }

    private fun submit() {
        val shared = file ?: return
        val selected = channel
        val selectedCall = callId
        busy = true
        io.execute {
            try {
                val result = ShareSubmission.submit(this, shared, selected, selectedCall)
                runOnUiThread {
                    file = null
                    busy = false
                    autoCallId?.let { AutoShareRuntime.onSubmitted(it); finish() }
                    message = if (result.duplicate) "이미 등록된 텍스트입니다."
                        else "Courier 등록 완료: ${result.manifest.captureId}. 전송은 동기화 폴더 연결 후 진행됩니다."
                }
            } catch (error: Exception) { showError(error) }
        }
    }

    private fun showError(error: Exception) = runOnUiThread {
        busy = false
        message = when (error) {
            is SecurityException -> "공유 파일 읽기 권한이 만료되었습니다. 원래 앱에서 다시 공유하세요."
            is CharacterCodingException -> "UTF-8 또는 BOM 있는 UTF-16 텍스트로 공유하세요. CP949·BOM 없는 UTF-16·손상된 인코딩은 지원하지 않습니다."
            is IllegalArgumentException, is IllegalStateException -> error.message ?: "공유 입력을 확인하세요."
            else -> "공유 텍스트를 읽거나 저장하지 못했습니다. 다시 공유하세요."
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        outState.putString("file", file?.absolutePath)
        outState.putString("channel", channel.name)
        outState.putString("callId", callId)
        super.onSaveInstanceState(outState)
    }

    override fun onDestroy() {
        if (isFinishing) file?.delete()
        io.shutdown()
        super.onDestroy()
    }
}
