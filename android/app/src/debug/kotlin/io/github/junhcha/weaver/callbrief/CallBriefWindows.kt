package io.github.junhcha.weaver.callbrief

import android.app.ActivityOptions
import android.app.KeyguardManager
import android.content.Context
import android.content.Intent
import android.graphics.Rect
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.compose.ui.graphics.toArgb
import io.github.junhcha.weaver.ui.WeaverTokens

/** Only the small top window intercepts taps; no full-screen touch surface. */
internal object CallBriefWindows {
    private val handler = Handler(Looper.getMainLooper())
    private var view: View? = null
    private var manager: WindowManager? = null
    val isVisible: Boolean get() = view != null
    var launchPending = false

    fun hide() {
        handler.removeCallbacksAndMessages(null)
        view?.let { runCatching { manager?.removeViewImmediate(it) } }
        view = null
        manager = null
    }

    private fun dp(context: Context, value: Int) = (value * context.resources.displayMetrics.density).toInt()

    private fun attach(context: Context, child: View, width: Int, height: Int): Boolean {
        hide()
        if (!Settings.canDrawOverlays(context) || locked(context)) return false
        val wm = context.getSystemService(WindowManager::class.java)
        val params = WindowManager.LayoutParams(width, height, WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
            PixelFormat.TRANSLUCENT).apply {
            gravity = Gravity.TOP or Gravity.END
            x = dp(context, 8)
            y = dp(context, 8)
        }
        return runCatching { wm.addView(child, params); manager = wm; view = child; true }.getOrDefault(false)
    }

    private fun text(context: Context, value: String) = TextView(context).apply {
        text = value
        textSize = WeaverTokens.number("typography.size.body")
        setTextColor(WeaverTokens.ink.toArgb())
        setPadding(dp(context, 12), dp(context, 6), dp(context, 12), dp(context, 6))
        background = GradientDrawable().apply {
            setColor(WeaverTokens.surface.toArgb())
            cornerRadius = dp(context, WeaverTokens.number("radius.card").toInt()).toFloat()
            setStroke(dp(context, 1), WeaverTokens.indigo.toArgb())
        }
    }

    fun strip(context: Context, phase: String, lookup: String): Boolean {
        val app = context.applicationContext
        val strip = text(app, "Weaver · $phase\n가상 브리핑 보기 · $lookup").apply {
            maxLines = 2
            contentDescription = "Weaver $phase, 가상 브리핑 열기"
            setOnClickListener { launch(app) }
            setOnLongClickListener { hide(); true }
        }
        val width = minOf(dp(app, 300), app.resources.displayMetrics.widthPixels - dp(app, 16))
        if (!attach(app, strip, width, dp(app, 64))) return false
        handler.postDelayed({
            val chip = text(app, "W").apply {
                gravity = Gravity.CENTER
                setPadding(0, 0, 0, 0)
                contentDescription = "Weaver 가상 브리핑 열기, 길게 눌러 닫기"
                setOnClickListener { launch(app) }
                setOnLongClickListener { hide(); true }
            }
            attach(app, chip, dp(app, 48), dp(app, 48))
            handler.postDelayed({ hide() }, 30_000)
        }, 3_000)
        return true
    }

    fun card(context: Context): Boolean {
        val app = context.applicationContext
        val column = LinearLayout(app).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(WeaverTokens.surface.toArgb())
        }
        val close = Button(app).apply {
            text = "가상 브리핑 · 닫기"
            isAllCaps = false
            setTextColor(WeaverTokens.indigo.toArgb())
            backgroundTintList = android.content.res.ColorStateList.valueOf(WeaverTokens.bg.toArgb())
            setOnClickListener { hide() }
        }
        column.addView(close, LinearLayout.LayoutParams(-1, dp(app, 48)))
        val scroll = ScrollView(app).apply { addView(text(app, "${FictionalBrief.title}\n${FictionalBrief.body}")) }
        column.addView(scroll, LinearLayout.LayoutParams(-1, 0, 1f))
        val height = minOf(dp(app, 200), app.resources.displayMetrics.heightPixels / 4)
        val width = minOf(dp(app, 320), app.resources.displayMetrics.widthPixels - dp(app, 16))
        if (!attach(app, column, width, height)) return false
        handler.postDelayed({ hide() }, 30_000)
        return true
    }

    fun locked(context: Context): Boolean =
        context.getSystemService(KeyguardManager::class.java).isKeyguardLocked ||
            !context.getSystemService(PowerManager::class.java).isInteractive

    fun launch(context: Context) {
        if (locked(context)) {
            hide(); CallBriefService.notifyBrief(context); return
        }
        hide()
        val metrics = context.resources.displayMetrics
        val bounds = Rect(dp(context, 16), dp(context, 64), metrics.widthPixels - dp(context, 16), minOf(metrics.heightPixels / 2, dp(context, 420)))
        val multiple = context.getSharedPreferences("callbrief", 0).getBoolean("multiple", true)
        val intent = Intent(context, CallBriefActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or
            if (multiple) Intent.FLAG_ACTIVITY_MULTIPLE_TASK else 0)
        launchPending = true
        runCatching { context.startActivity(intent, ActivityOptions.makeBasic().setLaunchBounds(bounds).toBundle()) }
            .onFailure { launchPending = false; if (!card(context)) CallBriefService.notifyBrief(context) }
        // Background launch restrictions can silently refuse a launch without throwing.
        handler.postDelayed({
            if (launchPending) {
                launchPending = false
                if (!card(context)) CallBriefService.notifyBrief(context)
            }
        }, 1_200)
    }
}
