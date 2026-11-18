package io.github.junhcha.weaver.callbrief

import android.Manifest
import android.app.*
import android.content.*
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.provider.ContactsContract
import android.telephony.TelephonyManager
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import io.github.junhcha.weaver.R
import java.util.concurrent.Executors

/** User start only; no accessibility service, server, call control, or persisted identity. */
class CallBriefService : Service() {
    private val session = CallSession()
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private var generation = 0
    private var registered = false
    private val receiver = object : BroadcastReceiver() {
        @Suppress("DEPRECATION")
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action == Intent.ACTION_SCREEN_OFF) {
                val hadWindow = CallBriefWindows.isVisible
                CallBriefWindows.hide()
                if (hadWindow || session.state != "IDLE") notifyBrief(this@CallBriefService)
                return
            }
            if (intent.action != TelephonyManager.ACTION_PHONE_STATE_CHANGED) return
            val next = intent.getStringExtra(TelephonyManager.EXTRA_STATE) ?: return
            val changed = session.accept(next, intent.getStringExtra(TelephonyManager.EXTRA_INCOMING_NUMBER))
            if (!changed) return
            // Coalesce number/blank duplicate broadcasts before querying contacts.
            val ticket = ++generation
            main.postDelayed({ if (ticket == generation) present() }, 250)
        }
    }

    override fun onCreate() {
        super.onCreate()
        ensureChannels(this)
        val stop = PendingIntent.getService(this, 2, Intent(this, CallBriefService::class.java).setAction("stop"), PendingIntent.FLAG_IMMUTABLE)
        val setup = PendingIntent.getActivity(this, 3, Intent(this, CallBriefSetupActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val notification = NotificationCompat.Builder(this, WATCH_CHANNEL)
            .setSmallIcon(R.drawable.ic_launcher_foreground).setContentTitle("Weaver 통화 시험 감시 중")
            .setContentText("가상 자료만 표시 · 눌러 설정").setContentIntent(setup).setOngoing(true)
            .addAction(0, "중지", stop).build()
        startForeground(40, notification)
        ContextCompat.registerReceiver(this, receiver, IntentFilter().apply {
            addAction(TelephonyManager.ACTION_PHONE_STATE_CHANGED)
            addAction(Intent.ACTION_SCREEN_OFF)
        }, ContextCompat.RECEIVER_EXPORTED)
        registered = true
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == "stop") stopSelf()
        return START_NOT_STICKY
    }

    private fun present() {
        val ticket = generation
        val number = session.number
        val phase = when (session.state) { "RINGING" -> "수신 중"; "OFFHOOK" -> "통화/발신 중"; else -> "통화 종료" }
        io.execute {
            val lookup = lookupName(number)
            main.post {
                if (ticket != generation || !registered) return@post
                if (CallBriefWindows.locked(this) || !CallBriefWindows.strip(this, phase, lookup)) {
                    CallBriefWindows.hide()
                    notifyBrief(this)
                }
            }
        }
    }

    private fun lookupName(number: String?): String {
        if (number.isNullOrBlank()) return "번호 미제공"
        if (checkSelfPermission(Manifest.permission.READ_CONTACTS) != PackageManager.PERMISSION_GRANTED) return "연락처 권한 없음"
        // Discard the real name: only synthetic brief content can reach a window.
        return runCatching {
            val uri = Uri.withAppendedPath(ContactsContract.PhoneLookup.CONTENT_FILTER_URI, Uri.encode(number))
            contentResolver.query(uri, arrayOf(ContactsContract.PhoneLookup.DISPLAY_NAME), null, null, null)?.use {
                if (it.moveToFirst() && !it.getString(0).isNullOrBlank()) "연락처 이름 해석 성공" else "연락처 없음"
            } ?: "연락처 없음"
        }.getOrDefault("연락처 조회 불가")
    }

    override fun onDestroy() {
        registered = false
        generation++
        main.removeCallbacksAndMessages(null)
        runCatching { unregisterReceiver(receiver) }
        io.shutdownNow()
        CallBriefWindows.hide()
        getSystemService(NotificationManager::class.java).cancel(41)
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        private const val WATCH_CHANNEL = "callbrief-watch"
        private const val BRIEF_CHANNEL = "callbrief-fictional"
        private fun ensureChannels(context: Context) {
            context.getSystemService(NotificationManager::class.java).apply {
                createNotificationChannel(NotificationChannel(WATCH_CHANNEL, "통화 시험 감시", NotificationManager.IMPORTANCE_LOW))
                createNotificationChannel(NotificationChannel(BRIEF_CHANNEL, "가상 통화 브리핑", NotificationManager.IMPORTANCE_DEFAULT))
            }
        }

        fun notifyBrief(context: Context) {
            ensureChannels(context)
            val pending = PendingIntent.getActivity(context, 1, Intent(context, CallBriefActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK), PendingIntent.FLAG_IMMUTABLE)
            val notification = NotificationCompat.Builder(context, BRIEF_CHANNEL)
                .setSmallIcon(R.drawable.ic_launcher_foreground).setContentTitle("Weaver · 가상 통화 브리핑")
                .setContentText("잠금 해제 후 시험용 브리핑 보기").setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
                .setContentIntent(pending).setAutoCancel(true).build()
            runCatching { context.getSystemService(NotificationManager::class.java).notify(41, notification) }
        }
    }
}
