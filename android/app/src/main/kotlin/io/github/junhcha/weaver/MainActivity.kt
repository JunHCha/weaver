package io.github.junhcha.weaver

import android.content.Intent
import android.database.ContentObserver
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.mutableStateOf
import androidx.lifecycle.lifecycleScope
import io.github.junhcha.weaver.courier.CourierRuntime
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.shepherd.ShepherdRuntime
import io.github.junhcha.weaver.ui.HomeScreen
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    private var observer: ContentObserver? = null
    private val showPending = mutableStateOf(false)
    private val showInbox = mutableStateOf(false)

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        showPending.value = intent.action == ShepherdRuntime.OPEN_PENDING
        showInbox.value = intent.action == InboxRuntime.OPEN_INBOX
    }

    override fun onPause() {
        observer?.let { contentResolver.unregisterContentObserver(it) }
        observer = null
        super.onPause()
    }

    override fun onResume() {
        super.onResume()
        CourierRuntime.schedule(this)
        lifecycleScope.launch(Dispatchers.IO) { ShepherdRuntime.schedule(applicationContext) }
        observer = ShepherdRuntime.observeForeground(this)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        showPending.value = intent.action == ShepherdRuntime.OPEN_PENDING
        showInbox.value = intent.action == InboxRuntime.OPEN_INBOX
        setContent { HomeScreen(showPending.value, showInbox.value) }
    }
}
