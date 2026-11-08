package io.github.junhcha.weaver

import android.os.Bundle
import android.content.Intent
import android.database.ContentObserver
import androidx.compose.runtime.mutableStateOf
import io.github.junhcha.weaver.shepherd.ShepherdRuntime
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import io.github.junhcha.weaver.ui.HomeScreen
import io.github.junhcha.weaver.courier.CourierRuntime

class MainActivity : ComponentActivity() {
    private var observer: ContentObserver? = null
    private val showPending = mutableStateOf(false)

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        showPending.value = intent.action == ShepherdRuntime.OPEN_PENDING
    }

    override fun onPause() {
        observer?.let { contentResolver.unregisterContentObserver(it) }
        observer = null
        super.onPause()
    }

    override fun onResume() {
        super.onResume()
        CourierRuntime.schedule(this)
        observer = ShepherdRuntime.observeForeground(this)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        showPending.value = intent.action == ShepherdRuntime.OPEN_PENDING
        setContent { HomeScreen(showPending.value) }
    }
}
