package io.github.junhcha.weaver.shepherd

import android.content.Context
import io.github.junhcha.weaver.courier.CourierDatabase
import io.github.junhcha.weaver.schema.Channel

/** #30 supplies the durable call list; implementations must make markProcessed idempotent. */
data class PendingCall(val id: String, val occurredAt: String?, val counterpart: String?)

interface PendingCallRepository {
    /** Newest first; excludes calls already linked to a successfully sent script. */
    fun pending(): List<PendingCall>
    fun markProcessed(callId: String, captureId: String)
}

object PendingCalls {
    /** Uses #30's public IO API; injectable only to keep tests independent of MediaStore. */
    var repository: (Context) -> PendingCallRepository = { context ->
        object : PendingCallRepository {
            override fun pending(): List<PendingCall> = ShepherdRuntime.pendingCalls(context).map {
                PendingCall(it.id, it.occurredAt, it.counterpart)
            }
            override fun markProcessed(callId: String, captureId: String) {
                ShepherdRuntime.markProcessed(context, callId)
            }
        }
    }

    fun reconcile(context: Context) {
        val prefs = context.getSharedPreferences("shepherd-call-links", Context.MODE_PRIVATE)
        val sent = CourierDatabase(context).use { db ->
            db.captures().filter { it.state != "PENDING" }.associateBy { it.manifest.captureId }
        }
        val drafts = context.getSharedPreferences("shepherd-call-drafts", Context.MODE_PRIVATE)
        CourierDatabase(context).use { db ->
            db.captures().forEach { capture ->
                val callId = drafts.getString(capture.manifest.sha256, null)
                if (callId != null && capture.manifest.channel == Channel.CALL) {
                    check(prefs.edit().putString(capture.manifest.captureId, callId).commit())
                    check(drafts.edit().remove(capture.manifest.sha256).commit())
                }
            }
        }
        prefs.all.forEach { (captureId, callId) ->
            if (captureId in sent && callId is String) {
                repository(context).markProcessed(callId, captureId)
                check(prefs.edit().remove(captureId).commit())
            }
        }
    }
}
