package io.github.junhcha.weaver

import android.content.Intent
import android.database.ContentObserver
import android.graphics.Color
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.mutableStateOf
import androidx.lifecycle.lifecycleScope
import io.github.junhcha.weaver.courier.CourierRuntime
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.phone.PhoneReadModels
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

    /** The notification tap has been applied; drop it so recreation does not replay it. */
    private fun consumeNotification() {
        showPending.value = false
        showInbox.value = false
        setIntent(Intent(intent).setAction(null))
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
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.light(Color.TRANSPARENT, Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.light(Color.TRANSPARENT, Color.TRANSPARENT),
        )
        showPending.value = intent.action == ShepherdRuntime.OPEN_PENDING
        showInbox.value = intent.action == InboxRuntime.OPEN_INBOX
        val readModel = PhoneReadModels.forContext(this, BuildConfig.DEBUG)
        setContent { HomeScreen(readModel, showPending.value, showInbox.value, ::consumeNotification) }
    }
}
