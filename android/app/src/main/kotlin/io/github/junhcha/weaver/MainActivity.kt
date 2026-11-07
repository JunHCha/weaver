package io.github.junhcha.weaver

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import io.github.junhcha.weaver.ui.HomeScreen
import io.github.junhcha.weaver.courier.CourierRuntime

class MainActivity : ComponentActivity() {
    override fun onResume() {
        super.onResume()
        CourierRuntime.schedule(this)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent { HomeScreen() }
    }
}
