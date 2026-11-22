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
import io.github.junhcha.weaver.courier.CourierDatabase
import io.github.junhcha.weaver.courier.CourierFolder
import io.github.junhcha.weaver.courier.CourierRules
import io.github.junhcha.weaver.courier.CourierRuntime
import io.github.junhcha.weaver.courier.CourierWorker
import io.github.junhcha.weaver.storage.AppStorage
import io.github.junhcha.weaver.inbox.InboxDatabase
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.schema.Alert
import java.io.File
import java.util.concurrent.TimeUnit
import kotlinx.serialization.json.JsonPrimitive

data class PairResult(val serverId: String, val otherServerQueued: Int, val alertWarning: String? = null)

/** Wires pairing, the server's routes, the per-server spool and the exchange into the existing Courier runtime. */
object TransportRuntime {
    /** Tests replace the HTTPS client with a local one; production builds the pinned client from the pairing. */
    @Volatile internal var apiFactory: (Pairing) -> MobileApi = { HttpsMobileApi(it) }
    @Volatile internal var secretBox: () -> SecretBox = { KeystoreSecretBox() }

    fun settings(context: Context): TransportSettings = TransportSettings(
        context.applicationContext.getSharedPreferences("transport", Context.MODE_PRIVATE), secretBox(),
    )

    private fun servers(context: Context) = File(AppStorage.exchangeDir(context), "http/servers")
    private fun safe(id: String) = id.replace(Regex("[^A-Za-z0-9_.-]"), "_")

    /** The server whose spool is active: the paired one, or the last one while unpaired (its items still have to move). */
    private fun serverDir(context: Context, settings: TransportSettings): File =
        File(servers(context), safe(settings.serverId ?: lastServer(context) ?: "unpaired"))

    private fun lastServer(context: Context): String? =
        context.getSharedPreferences("transport-last", Context.MODE_PRIVATE).getString("server_id", null)

    /** A spool belongs to one server ID: another server never sees these items, and they never see its. */
    fun spool(context: Context, settings: TransportSettings = settings(context)) =
        SpoolFolder(File(serverDir(context, settings), "spool"), "http:${settings.serverId ?: lastServer(context) ?: "unpaired"}")
    fun state(context: Context, settings: TransportSettings = settings(context)) = TransportState(File(serverDir(context, settings), "state.json"))

    fun routed(context: Context, files: CourierFolder): RoutedFolder {
        val settings = settings(context)
        val spool = spool(context, settings)
        return RoutedFolder(files, spool, settings::route, settings::feature, spool.identity)
    }

    /** Null when nothing is routed over HTTP, there is no pairing, or the phone must pair again. */
    fun exchange(context: Context): HttpExchange? {
        val settings = settings(context)
        if (!settings.anyExchange || settings.needsRepair) return null
        val pairing = settings.pairing() ?: return null
        return HttpExchange(apiFactory(pairing), spool(context, settings), state(context, settings), settings::route, { feature -> settings.feature(feature) && (feature != ServerFeature.ALERTS || settings.alertError == null) },
            inboxAlertLedger(context, pairing.serverId, pairing.deviceId), feedSession(context, settings))
    }

    /** This server's feed position. It survives unpairing with the rest of the server's private data. */
    fun feedStore(context: Context, settings: TransportSettings = settings(context)) = FeedStore(File(serverDir(context, settings), "feed.json"))

    /** The HTTP copy of the map for this server (data layer; the map screen does not read it yet). */
    fun pipelineCache(context: Context, settings: TransportSettings = settings(context)) =
        io.github.junhcha.weaver.pipeline.PipelineCache(File(serverDir(context, settings), "pipeline-cache"))

    /**
     * The kinds this phone consumes over HTTP right now: a kind whose route is files is left out (the server also hides
     * it), so one flow is never handled by both paths. Fabric and wiki hints only tell the read model to refetch,
     * which it does on its own, so they are not requested.
     */
    internal fun requestedFeedKinds(settings: TransportSettings): Set<String> {
        val served = settings.feedKinds
        val wanted = buildSet {
            if (settings.route(TransportFeature.VERDICT) == TransportRoute.HTTP) add("review")
            if (settings.route(TransportFeature.ACTION_RESULT) == TransportRoute.HTTP) add("action")
            if (settings.route(TransportFeature.ALERT) == TransportRoute.HTTP && settings.feature(ServerFeature.ALERTS) && settings.alertError == null) add("alert")
            if (settings.pipelineHttp) add("pipeline")
        }
        return wanted.filter { it in served && it in SUPPORTED_FEED_KINDS }.toSet()
    }

    private fun feedSession(context: Context, settings: TransportSettings): FeedSession? {
        val requested = requestedFeedKinds(settings)
        return if (requested.isEmpty()) null else FeedSession(feedStore(context, settings), pipelineCache(context, settings), requested)
    }

    internal fun inboxAlertLedger(context: Context, serverId: String, deviceId: String): AlertLedger = object : AlertLedger {
        private fun <T> local(block: (InboxDatabase) -> T): T =
            synchronized(InboxRuntime.lock) { InboxDatabase(context).use(block) }
        override fun receive(alert: Alert) { local { it.receiveAlert(serverId, alert, System.currentTimeMillis(), deviceId) } }
        override fun pending(): List<String> = local { it.pendingAlertAcks(serverId, deviceId) }
        override fun acknowledged(alertId: String) { local { it.alertAcknowledged(serverId, deviceId, alertId) } }
    }

    /** The paired server's API for reads and tag edits, or null when this phone is not paired (or must pair again). */
    fun api(context: Context): MobileApi? {
        val settings = settings(context)
        if (settings.needsRepair) return null
        return settings.pairing()?.let(apiFactory)
    }

    fun finished(context: Context, summary: ExchangeSummary) {
        val settings = settings(context)
        if (summary.needsRepair) settings.needsRepair = true
        settings.lastError = summary.errorCode
        if (summary.rejected.isNotEmpty()) CourierDatabase(context).use { db ->
            summary.rejected.forEach { (key, code) -> db.diagnostic("http", key, code) }
        }
        if (!summary.unreachable && !summary.needsRepair) settings.lastSyncAt = System.currentTimeMillis()
    }

    /**
     * Reads `capabilities` and follows the server: transports per feature and the optional features. The phone never
     * switches on its own. A route that changed moves the unsent items with it ([migrateOrphans]). Failures keep the
     * last known routes and are reported in the returned summary.
     */
    fun refresh(context: Context): ExchangeSummary {
        val summary = ExchangeSummary()
        val settings = settings(context)
        if (!settings.paired || settings.needsRepair) return summary
        val pairing = settings.pairing() ?: run { summary.needsRepair = true; summary.fail("token_unreadable", false); return summary }
        try {
            val capabilities = apiFactory(pairing).capabilities()
            val identityBefore = routed(context, MissingFolder).identity
            applyServerCapabilities(context, pairing.serverId, capabilities)?.let { summary.fail(it, false) }
            val after = routed(context, MissingFolder).identity
            if (identityBefore != after) io.github.junhcha.weaver.review.ReviewRuntime.retarget(context, identityBefore, after)
        } catch (error: IllegalStateException) { summary.fail(error.message ?: "alert_source_conflict", false)
        } catch (error: ApiException) {
            if (error.status == 401 || error.code == "node_mismatch") summary.needsRepair = true
            summary.fail(error.code, error.retryable)
        } catch (error: PinMismatchException) { summary.needsRepair = true; summary.fail("pin_mismatch", false)
        } catch (error: InvalidResponseException) { summary.fail("invalid_read_response", false)
        } catch (error: UnreachableException) { summary.unreachable = true; summary.fail("unreachable", true) }
        return summary
    }

    /**
     * Pairs with the server from a QR/pasted [text]; fails before touching any setting if the server is wrong. Another
     * server ID starts a separate spool, and the result says how many items the previous server still holds.
     */
    fun pair(context: Context, text: String): PairResult {
        val pairing = PairingCodec.parse(text)
        val capabilities = apiFactory(pairing).capabilities()
        val reported = (capabilities["server_id"] as? JsonPrimitive)?.content
        if (reported != pairing.serverId) throw PairingException("서버가 코드의 서버와 달라요")
        val settings = settings(context)
        synchronized(CourierRuntime.lock) {
            settings.pair(pairing)
            applyServerCapabilities(context, pairing.serverId, capabilities)
            context.getSharedPreferences("transport-last", Context.MODE_PRIVATE).edit().putString("server_id", pairing.serverId).commit()
        }
        return PairResult(pairing.serverId, otherServerQueue(context), settings.alertError?.let(::alertPairingWarning))
    }

    internal fun alertPairingWarning(code: String): String = when (code) {
        "alert_source_server_conflict" -> "연결했지만 변경 알림 폴더가 다른 서버에 연결되어 있어요. 서버별 폴더를 선택하세요."
        "alert_body_conflict" -> "연결했지만 같은 변경 알림 ID의 본문이 달라요. 알림 사본을 확인하세요."
        else -> "연결했지만 변경 알림 저장소를 열지 못했어요. 다시 동기화해 주세요."
    }

    internal fun applyServerCapabilities(context: Context, serverId: String, capabilities: kotlinx.serialization.json.JsonObject,
                                         files: CourierFolder? = null): String? {
        val settings = settings(context)
        settings.applyCapabilities(capabilities)
        val error = try {
            val alertFiles = files ?: CourierRuntime.files(context)
            if (alertFiles !== MissingFolder) synchronized(InboxRuntime.lock) {
                InboxDatabase(context).use { it.bindAlertSource(alertFiles.identity, serverId) }
            }
            null
        } catch (error: IllegalStateException) {
            error.message?.takeIf { it in setOf("alert_source_server_conflict", "alert_body_conflict") } ?: "alert_store_unavailable"
        } catch (_: Exception) { "alert_store_unavailable" }
        settings.alertError = error
        return error
    }

    /** Queued items that belong to a server other than the paired one (kept, never sent to the wrong server). */
    fun otherServerQueue(context: Context): Int = synchronized(CourierRuntime.lock) {
        val current = settings(context).serverId?.let(::safe)
        servers(context).listFiles().orEmpty().filter { it.isDirectory && it.name != current }
            .sumOf { HttpExchange.queue(SpoolFolder(File(it, "spool")), TransportState(File(it, "state.json"))).total }
    }

    /** Forgets the server. Its queued items move back to the Syncthing folder with their original IDs. */
    fun unpair(context: Context) {
        synchronized(CourierRuntime.lock) {
            val settings = settings(context)
            val before = routed(context, MissingFolder).identity
            settings.unpair()
            val after = routed(context, MissingFolder).identity
            if (before != after) io.github.junhcha.weaver.review.ReviewRuntime.retarget(context, before, after)
            migrateOrphans(context, CourierRuntime.files(context))
        }
    }

    fun queue(context: Context): QueueCounts = synchronized(CourierRuntime.lock) {
        val settings = settings(context)
        HttpExchange.queue(spool(context, settings), state(context, settings))
    }

    /**
     * Moves unsent items out of spools nobody drains any more: a feature the server switched back to Syncthing, or every
     * feature after unpairing. Names (= IDs) and bytes are unchanged; payloads go before their manifests. Nothing moves
     * while the Syncthing folder is unavailable, and the spool copy is deleted only after the folder holds it, so
     * an item is never lost, only moved later. Items of another paired server stay where they are.
     */
    fun migrateOrphans(context: Context, files: CourierFolder): Int = synchronized(CourierRuntime.lock) {
        if (files === MissingFolder) return 0
        val settings = settings(context)
        var moved = 0
        servers(context).listFiles().orEmpty().filter { it.isDirectory }.forEach { dir ->
            val active = settings.serverId?.let(::safe)
            if (active != null && dir.name != active) return@forEach
            val spool = SpoolFolder(File(dir, "spool"))
            val state = TransportState(File(dir, "state.json"))
            TransportFeature.entries.forEach { feature ->
                if (active != null && settings.route(feature) == TransportRoute.HTTP) return@forEach
                val folder = when (feature) {
                    TransportFeature.CAPTURE -> "outbox"
                    TransportFeature.VERDICT -> "decisions"
                    TransportFeature.ACTION_RESULT -> CourierRules.ACTION_RESULTS
                    TransportFeature.ALERT -> return@forEach
                }
                val entries = spool.list(folder).filter { it.regular && !CourierRules.ignored(it.name) }
                // Payloads first, manifests (.json) last, so a crash between the two never exposes a manifest without its body.
                (entries.filter { !it.name.endsWith(".json") } + entries.filter { it.name.endsWith(".json") }).forEach { entry ->
                    try {
                        val bytes = spool.read(folder, entry.name).use { it.readBytes() }
                        val present = files.list(folder).any { it.name == entry.name }
                        if (!present) files.publish(folder, entry.name) { it.write(bytes) }
                        else check(files.read(folder, entry.name).use { it.readBytes() }.contentEquals(bytes)) { "existing_differs" }
                        spool.delete(folder, entry.name)
                        state.update {
                            it.copy(verdictOps = it.verdictOps - entry.name, resultOps = it.resultOps - entry.name.removeSuffix(".json"))
                        }
                        moved++
                    } catch (error: SecurityException) { throw error
                    } catch (_: Exception) { /* kept in the spool; tried again at the next run */ }
                }
            }
        }
        moved
    }

    /** "Retry now" clears item blocks only. A rejected token or node still needs a new pairing. */
    fun retryBlocked(context: Context) {
        synchronized(CourierRuntime.lock) {
            val settings = settings(context)
            state(context, settings).update { it.copy(blocked = emptyMap(), blockedFeatures = emptyMap()) }
            // The feed may have moved past entries whose work was blocked: take a new snapshot so they run again.
            feedStore(context, settings).reset()
        }
        kick(context)
    }

    fun schedule(context: Context) {
        if (!settings(context).paired) return
        WorkManager.getInstance(context).enqueueUniquePeriodicWork("transport-periodic", ExistingPeriodicWorkPolicy.KEEP,
            PeriodicWorkRequestBuilder<TransportWorker>(15, TimeUnit.MINUTES)
                .setConstraints(connected()).setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS).build())
    }

    /** App start, foreground and manual refresh. Backoff is exponential from 30 s (WorkManager caps it at 5 h). */
    fun kick(context: Context) {
        if (!settings(context).paired) return
        WorkManager.getInstance(context).enqueue(
            OneTimeWorkRequestBuilder<TransportWorker>().addTag("transport-immediate").setConstraints(connected())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS).build())
    }

    /** CONNECTED only says there is a network; reaching the Mac is checked by the request's own timeout. */
    private fun connected() = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
}

/** Same pass as the Courier worker, but with the HTTP exchange talking to the server; runs only with a network. */
class TransportWorker(context: Context, params: WorkerParameters) : Worker(context, params) {
    override fun doWork(): Result {
        var crashRetry = false
        try { io.github.junhcha.weaver.diagnostics.CrashDiagnostics.send(applicationContext) }
        catch (error: ApiException) {
            if (error.status == 401 || error.code == "node_mismatch") TransportRuntime.settings(applicationContext).needsRepair = true
            crashRetry = error.retryable
        } catch (_: PinMismatchException) { TransportRuntime.settings(applicationContext).needsRepair = true
        } catch (_: InvalidResponseException) { /* keep for manual retry */
        } catch (_: Exception) { crashRetry = true }
        val courier = CourierWorker.run(applicationContext, network = true)
        return if (crashRetry && courier == Result.success()) Result.retry() else courier
    }
}
