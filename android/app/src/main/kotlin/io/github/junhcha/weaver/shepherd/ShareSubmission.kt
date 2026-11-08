package io.github.junhcha.weaver.shepherd

import android.content.Context
import android.net.Uri
import io.github.junhcha.weaver.courier.CourierDatabase
import io.github.junhcha.weaver.courier.CourierRuntime
import io.github.junhcha.weaver.schema.CaptureKind
import io.github.junhcha.weaver.schema.Channel
import io.github.junhcha.weaver.schema.Manifest
import java.io.File

object ShareSubmission {
    fun submit(context: Context, file: File, channel: Channel, callId: String?): Manifest = synchronized(CourierRuntime.lock) {
        require(channel in listOf(Channel.SHARE_TEXT, Channel.KAKAO, Channel.CALL))
        SharedText.validate(file)
        val hash = SharedText.digest(file)
        val existing = CourierDatabase(context).use { db -> db.captures().firstOrNull { it.manifest.sha256 == hash } }
        if (existing != null) {
            require(existing.manifest.channel == channel) { "이미 다른 종류로 등록된 텍스트입니다." }
            if (channel == Channel.CALL) {
                // A replay must never consume the next pending call.
                val assigned = context.getSharedPreferences("shepherd-call-assignments", Context.MODE_PRIVATE)
                    .getString(hash, null)
                require(callId != null && assigned == callId) { "이미 등록한 통화 스크립트입니다." }
            }
            CourierRuntime.kick(context)
            file.delete()
            return@synchronized existing.manifest
        }
        val call = if (channel == Channel.CALL) PendingCalls.repository(context).pending()
            .firstOrNull { it.id == callId } ?: error("연결할 미처리 통화가 없습니다. 저장하지 않았습니다.") else null
        // The durable intent survives death between queueing and link persistence.
        val links = context.getSharedPreferences("shepherd-call-drafts", Context.MODE_PRIVATE)
        if (call != null) {
            val reserved = context.getSharedPreferences("shepherd-call-links", Context.MODE_PRIVATE).all.values
            require(call.id !in reserved) { "이 통화의 스크립트가 이미 전송 대기 중입니다." }
            check(links.edit().putString(hash, call.id).commit())
            check(context.getSharedPreferences("shepherd-call-assignments", Context.MODE_PRIVATE).edit()
                .putString(hash, call.id).commit())
        }
        val manifest = CourierRuntime.prepare(context, Uri.fromFile(file), CaptureKind.TEXT, channel, "txt",
            call?.occurredAt, call?.counterpart)
        if (call != null) {
            check(context.getSharedPreferences("shepherd-call-links", Context.MODE_PRIVATE).edit()
                .putString(manifest.captureId, call.id).commit())
        }
        links.edit().remove(hash).commit()
        file.delete()
        manifest
    }
}
