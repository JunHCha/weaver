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
import java.io.File
import java.util.concurrent.TimeUnit
import kotlinx.serialization.json.JsonPrimitive

data class PairResult(val serverId: String, val otherServerQueued: Int)

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
        if (!settings.anyHttp || settings.needsRepair) return null
        val pairing = settings.pairing() ?: return null
        return HttpExchange(apiFactory(pairing), spool(context, settings), state(context, settings), settings::route, settings::feature)
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
            settings.applyCapabilities(capabilities)
            val after = routed(context, MissingFolder).identity
            if (identityBefore != after) io.github.junhcha.weaver.review.ReviewRuntime.retarget(context, identityBefore, after)
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
            settings.applyCapabilities(capabilities)
            context.getSharedPreferences("transport-last", Context.MODE_PRIVATE).edit().putString("server_id", pairing.serverId).commit()
        }
        return PairResult(pairing.serverId, otherServerQueue(context))
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
