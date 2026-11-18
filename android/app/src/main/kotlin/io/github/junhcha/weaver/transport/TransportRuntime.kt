package io.github.junhcha.weaver.transport

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import io.github.junhcha.weaver.courier.CourierFolder
import io.github.junhcha.weaver.courier.CourierRuntime
import io.github.junhcha.weaver.courier.CourierWorker
import io.github.junhcha.weaver.storage.AppStorage
import java.io.File
import java.util.concurrent.TimeUnit

/** Wires pairing, routes, the spool and the exchange into the existing Courier runtime. */
object TransportRuntime {
    /** Tests replace the HTTPS client with a local one; production builds the pinned client from the pairing. */
    @Volatile internal var apiFactory: (Pairing) -> MobileApi = { HttpsMobileApi(it) }
    @Volatile internal var secretBox: () -> SecretBox = { KeystoreSecretBox() }

    fun settings(context: Context): TransportSettings = TransportSettings(
        context.applicationContext.getSharedPreferences("transport", Context.MODE_PRIVATE), secretBox(),
    )

    private fun directory(context: Context) = File(AppStorage.exchangeDir(context), "http")
    fun spool(context: Context) = SpoolFolder(File(directory(context), "spool"))
    fun state(context: Context) = TransportState(File(directory(context), "state.json"))

    fun routed(context: Context, files: CourierFolder): RoutedFolder {
        val settings = settings(context)
        return RoutedFolder(files, spool(context), settings::route)
    }

    /** The paired server's API for reads and tag edits, or null when this phone is not paired (or must pair again). */
    fun api(context: Context): MobileApi? {
        val settings = settings(context)
        if (settings.needsRepair) return null
        return settings.pairing()?.let(apiFactory)
    }

    /** Null when nothing is routed over HTTP, there is no pairing, or the server rejected the token. */
    fun exchange(context: Context): HttpExchange? {
        val settings = settings(context)
        if (!settings.anyHttp || settings.needsRepair) return null
        val pairing = settings.pairing() ?: return null
        return HttpExchange(apiFactory(pairing), spool(context), state(context), settings::route)
    }

    fun finished(context: Context, summary: ExchangeSummary) {
        val settings = settings(context)
        if (summary.needsRepair) settings.needsRepair = true
        settings.lastError = summary.errorCode
        if (!summary.unreachable && !summary.needsRepair) settings.lastSyncAt = System.currentTimeMillis()
    }

    /** Pairs with the server from a QR/pasted [text]; fails before touching any setting if the server is wrong. */
    fun pair(context: Context, text: String): String {
        val pairing = PairingCodec.parse(text)
        val api = apiFactory(pairing)
        val capabilities = api.capabilities()
        val serverId = (capabilities["server_id"] as? kotlinx.serialization.json.JsonPrimitive)?.content
            ?: throw PairingException("서버가 응답하지 않았습니다")
        val settings = settings(context)
        settings.pair(pairing)
        settings.serverId = serverId
        settings.lastError = null
        return serverId
    }

    /** The user's route choice. Unsent verdicts follow it so none is stranded on the old transport. */
    fun setRoute(context: Context, feature: TransportFeature, route: TransportRoute) {
        synchronized(CourierRuntime.lock) {
            val settings = settings(context)
            val files = CourierRuntime.files(context)
            val oldIdentity = routed(context, files).identity
            settings.setRoute(feature, route)
            val newIdentity = routed(context, files).identity
            if (oldIdentity != newIdentity) io.github.junhcha.weaver.review.ReviewRuntime.retarget(context, oldIdentity, newIdentity)
        }
        CourierRuntime.schedule(context)
    }

    fun unpair(context: Context) {
        synchronized(CourierRuntime.lock) { settings(context).unpair() }
    }

    fun queue(context: Context): QueueCounts = synchronized(CourierRuntime.lock) { HttpExchange.queue(spool(context), state(context)) }

    /** "Retry now" after a blocked item: clears the blocks and starts a run. */
    fun retryBlocked(context: Context) {
        synchronized(CourierRuntime.lock) { state(context).update { it.copy(blocked = emptyMap()) }; settings(context).needsRepair = false }
        kick(context)
    }

    fun schedule(context: Context) {
        if (!settings(context).anyHttp) return
        WorkManager.getInstance(context).enqueueUniquePeriodicWork("transport-periodic", ExistingPeriodicWorkPolicy.KEEP,
            PeriodicWorkRequestBuilder<TransportWorker>(15, TimeUnit.MINUTES)
                .setConstraints(connected()).setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS).build())
    }

    /** App start, foreground and manual refresh. Backoff is exponential from 30 s (WorkManager caps it at 5 h). */
    fun kick(context: Context) {
        if (!settings(context).anyHttp) return
        WorkManager.getInstance(context).enqueue(
            OneTimeWorkRequestBuilder<TransportWorker>().addTag("transport-immediate").setConstraints(connected())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS).build())
    }

    /** CONNECTED only says there is a network; reaching the Mac is checked by the request's own timeout. */
    private fun connected() = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
}

/** Same pass as the Courier worker, but with the HTTP exchange talking to the server; runs only with a network. */
class TransportWorker(context: Context, params: WorkerParameters) : Worker(context, params) {
    override fun doWork(): Result = CourierWorker.run(applicationContext, network = true)
}
