package io.github.junhcha.weaver.review

import android.content.Context
import io.github.junhcha.weaver.courier.CourierFolder
import io.github.junhcha.weaver.courier.CourierRuntime
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.schema.ReviewPackage
import io.github.junhcha.weaver.schema.Verdict
import io.github.junhcha.weaver.storage.AppStorage
import java.io.File

/** IO entry points; serializes UI actions and WorkManager with Courier's process lock. */
object ReviewRuntime {
    private fun store(context: Context) = ReviewStore(File(AppStorage.exchangeDir(context), "reviews"))
    fun records(context: Context): List<ReviewRecord> = synchronized(CourierRuntime.lock) { store(context).records() }
    fun open(context: Context, id: String): ReviewPackage = synchronized(CourierRuntime.lock) {
        val p = store(context).latest(CourierRuntime.folder(context), id)
        val known = InboxRuntime.reviews(context).firstOrNull { it.packageId == id }
        check(known == null || p.revision >= known.revision) { "최신 revision의 동기화를 기다리세요" }
        p
    }
    fun submit(context: Context, p: ReviewPackage, verdict: Verdict) = synchronized(CourierRuntime.lock) {
        check(open(context, p.packageId) == p) { "새 revision이 도착했습니다. 다시 확인하세요" }
        store(context).queue(CourierRuntime.folder(context), p, verdict)
        InboxRuntime.submitted(context, p.packageId, p.revision)
        CourierRuntime.kick(context)
        InboxRuntime.refresh(context)
    }
    fun synchronize(context: Context, folder: CourierFolder): Boolean = synchronized(CourierRuntime.lock) {
        val store = store(context)
        InboxRuntime.reviews(context).forEach { store.observeRevision(it.packageId, it.revision) }
        store.synchronize(folder).also { InboxRuntime.refresh(context) }
    }
    fun retry(context: Context) { CourierRuntime.kick(context) }
}
