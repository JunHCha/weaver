package io.github.junhcha.weaver.shepherd

import android.content.Context
import io.github.junhcha.weaver.courier.CourierClient
import io.github.junhcha.weaver.courier.CourierDatabase
import io.github.junhcha.weaver.schema.Channel
import java.time.OffsetDateTime

/** #30 supplies the durable call list. */
data class PendingCall(val id: String, val occurredAt: String?, val counterpart: String?, val displayName: String = "통화")

interface PendingCallRepository {
    fun pending(): List<PendingCall>
    fun markProcessed(callId: String, captureId: String): Boolean
}

object PendingCalls {
    internal fun adapt(records: List<CallRecord>): List<PendingCall> = records
        .filter { it.status == CallStatus.UNSHARED }
        .map { PendingCall(it.id, validTime(it.occurredAt), it.counterpart?.trim()?.takeIf(String::isNotEmpty), it.displayName) }
        .sortedWith(compareByDescending { it.occurredAt?.let(OffsetDateTime::parse)?.toInstant() })

    internal fun validTime(value: String?): String? = value?.takeIf {
        runCatching { io.github.junhcha.weaver.schema.Rules.timestamp(it, "occurred_at") }.isSuccess
    }

    var repository: (Context) -> PendingCallRepository = { context ->
        object : PendingCallRepository {
            override fun pending(): List<PendingCall> = adapt(ShepherdRuntime.pendingCalls(context))
            override fun markProcessed(callId: String, captureId: String): Boolean =
                ShepherdRuntime.markProcessed(context, callId)
        }
    }

    /** Recover only drafts that have a durable capture; an abandoned draft reserves nothing. */
    fun recoverLinks(context: Context) {
        val links = context.getSharedPreferences("shepherd-call-links", Context.MODE_PRIVATE)
        val drafts = context.getSharedPreferences("shepherd-call-drafts", Context.MODE_PRIVATE)
        CourierDatabase(context).use { db ->
            db.captures().forEach { capture ->
                val callId = drafts.getString(capture.manifest.sha256, null)
                if (callId != null && capture.manifest.channel == Channel.CALL) {
                    check(links.edit().putString(capture.manifest.captureId, callId).commit())
                    check(drafts.edit().remove(capture.manifest.sha256).commit())
                }
            }
        }
    }

    /** A failed call never prevents the remaining calls (or Courier verdicts) from progressing. */
    fun reconcile(context: Context): Boolean {
        recoverLinks(context)
        val prefs = context.getSharedPreferences("shepherd-call-links", Context.MODE_PRIVATE)
        val sent = CourierDatabase(context).use { db ->
            db.captures().filter { it.state != "PENDING" }.map { it.manifest.captureId }.toSet()
        }
        var successful = true
        prefs.all.forEach { (captureId, callId) ->
            if (captureId in sent && callId is String) {
                try {
                    if (repository(context).markProcessed(callId, captureId)) {
                        check(prefs.edit().remove(captureId).commit())
                    } else successful = false
                } catch (error: Exception) {
                    CourierDatabase(context).use { it.diagnostic("shepherd", "reconcile", CourierClient.errorCode(error)) }
                    successful = false
                }
            }
        }
        return successful
    }
}
