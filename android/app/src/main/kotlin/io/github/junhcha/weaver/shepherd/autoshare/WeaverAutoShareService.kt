package io.github.junhcha.weaver.shepherd.autoshare

import android.accessibilityservice.AccessibilityService
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import androidx.core.content.ContextCompat
import java.util.concurrent.Executors

/**
 * Presses the Voice Recorder's own share buttons for one queued call. Idle unless a job is running,
 * touches only [Selectors.allowedPackages], and never logs or stores screen text.
 */
class WeaverAutoShareService : AccessibilityService() {
    private val handler = Handler(Looper.getMainLooper())
    private val io = Executors.newSingleThreadExecutor()
    private var running: Running? = null

    private class Running(val active: AutoShareRuntime.ActiveJob, val machine: AutoShareMachine)

    private val tick = Runnable { step() }
    private val wake = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) { startNext() }
    }

    override fun onServiceConnected() {
        instance = this
        val filter = IntentFilter().apply {
            addAction(Intent.ACTION_USER_PRESENT)
            addAction(Intent.ACTION_SCREEN_ON)
        }
        ContextCompat.registerReceiver(this, wake, filter, ContextCompat.RECEIVER_NOT_EXPORTED)
        startNext()
    }

    private fun startNext() {
        io.execute { runCatching { AutoShareRuntime.maybeStart(applicationContext) } }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent) {
        val current = running ?: return
        if (event.eventType == AccessibilityEvent.TYPE_TOUCH_INTERACTION_START) {
            finish(current, current.machine.interrupt(AutoError.USER_TOUCH))
        } else {
            handler.removeCallbacks(tick)
            handler.post(tick)
        }
    }

    override fun onInterrupt() = Unit

    override fun onUnbind(intent: Intent?): Boolean {
        instance = null
        runCatching { unregisterReceiver(wake) }
        handler.post {
            running?.let { finish(it, it.machine.interrupt(AutoError.SERVICE_STOPPED)) }
        }
        return super.onUnbind(intent)
    }

    private fun launch(job: AutoShareRuntime.ActiveJob): Boolean {
        val machine = AutoShareMachine(job.job, SystemClock.elapsedRealtime(), { job.submitted })
        val state = Running(job, machine)
        val intent = packageManager.getLaunchIntentForPackage(Selectors.VOICE_NOTE)
            ?.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
        running = state
        val started = intent != null && runCatching { startActivity(intent) }.isSuccess
        if (!started) { finish(state, machine.interrupt(AutoError.LAUNCH_FAILED)); return true }
        handler.postDelayed(tick, TICK_MS)
        return true
    }

    private fun step() {
        val current = running ?: return
        val shot = snapshot()
        val progress = current.machine.advance(shot, SystemClock.elapsedRealtime())
        if (progress == Progress.Working) handler.postDelayed(tick, TICK_MS) else finish(current, progress)
    }

    private fun finish(current: Running, progress: Progress) {
        if (running !== current) return
        running = null
        handler.removeCallbacks(tick)
        val restore = progress is Progress.Succeeded || (progress is Progress.Failed && progress.error.restoreScreen)
        // Return the user to where they were; never after they took over the screen.
        if (restore) performGlobalAction(GLOBAL_ACTION_HOME)
        val app = applicationContext
        io.execute { AutoShareRuntime.onFinished(app, current.active.job, progress) }
    }

    private fun snapshot(): Screen? {
        val activeRoot = rootInActiveWindow
        val roots = windows.mapNotNull { it.root }.ifEmpty { listOfNotNull(activeRoot) }
        if (roots.isEmpty() && activeRoot == null) return null
        return Screen(roots.map { NodeAdapter(it, this) }, activeRoot?.packageName?.toString()) {
            performGlobalAction(GLOBAL_ACTION_BACK)
        }
    }

    /** Defense in depth: even a machine bug cannot click outside the allowed packages or without a job. */
    private class NodeAdapter(private val node: AccessibilityNodeInfo, private val service: WeaverAutoShareService) : UiNode {
        override val packageName get() = node.packageName?.toString()
        override val id: String? get() = node.viewIdResourceName
        override val text get() = node.text?.toString()
        override val contentDescription get() = node.contentDescription?.toString()
        override val clickable get() = node.isClickable
        override val scrollable get() = node.isScrollable
        override val visible get() = node.isVisibleToUser
        override val children get() = (0 until node.childCount).mapNotNull { node.getChild(it) }.map { NodeAdapter(it, service) }
        override val parent get() = node.parent?.let { NodeAdapter(it, service) }
        private fun permitted() = service.running != null && packageName in Selectors.allowedPackages
        override fun click() = permitted() && node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
        override fun scroll() = permitted() && node.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD)
        // Each parent/child access wraps a fresh AccessibilityNodeInfo; equality must follow the real node.
        override fun equals(other: Any?) = other is NodeAdapter && other.node == node
        override fun hashCode() = node.hashCode()
    }

    override fun onDestroy() {
        instance = null
        io.shutdown()
        super.onDestroy()
    }

    companion object {
        private const val TICK_MS = 500L
        @Volatile private var instance: WeaverAutoShareService? = null

        /** True when the service accepted the job. Thread-safe; the work itself runs on the main thread. */
        fun begin(job: AutoShareRuntime.ActiveJob): Boolean {
            val service = instance ?: return false
            service.handler.post { service.launch(job) }
            return true
        }
    }
}
