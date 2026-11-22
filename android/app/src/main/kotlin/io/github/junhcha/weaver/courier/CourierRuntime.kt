package io.github.junhcha.weaver.courier

import android.Manifest as AndroidManifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.DocumentsContract
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import androidx.work.*
import io.github.junhcha.weaver.MainActivity
import io.github.junhcha.weaver.R
import io.github.junhcha.weaver.schema.*
import io.github.junhcha.weaver.transport.MissingFolder
import io.github.junhcha.weaver.transport.TransportRuntime
import java.io.File
import java.io.FileOutputStream
import java.math.BigInteger
import java.security.SecureRandom
import java.time.Instant
import java.util.UUID
import java.util.concurrent.TimeUnit

/** IO-only API. The process lock serializes periodic, immediate, capture and diagnostic writes. */
object CourierRuntime {
    val lock = Any()
    const val AUTO_CLEANUP = false
    private fun prefs(context: Context) = context.getSharedPreferences("courier", Context.MODE_PRIVATE)
    fun tree(context: Context): Uri? = prefs(context).getString("tree", null)?.let(Uri::parse)
    fun stopped(context: Context): Boolean = prefs(context).getBoolean("stopped", false)
    fun status(context: Context): String = prefs(context).getString("status", "Courier 폴더를 선택하세요")!!

    fun picker(context: Context): Intent = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE).apply {
        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION or
            Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION or Intent.FLAG_GRANT_PREFIX_URI_PERMISSION)
        putExtra(DocumentsContract.EXTRA_INITIAL_URI, tree(context) ?: Uri.parse(
            "content://com.android.externalstorage.documents/document/primary%3ADocuments%2FWeaverCourier",
        ))
    }

    fun select(context: Context, uri: Uri, flags: Int): List<String> = synchronized(lock) {
        check(prefs(context).getString("unsafeTree", null) != uri.toString()) {
            "원자성 시험 실패: SAF 지원 보류. File API 전환 후 다시 시험하세요."
        }
        require(SafCourierFolder.allowed(uri)) { "primary Documents/WeaverCourier 폴더만 지원합니다" }
        val grant = flags and (Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
        require(grant == (Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION))
        context.contentResolver.takePersistableUriPermission(uri, grant)
        try {
            val folder = SafCourierFolder(context.contentResolver, uri)
            folder.checkPermission()
            val report = folder.selfTest()
            val old = tree(context)
            prefs(context).edit().putString("tree", uri.toString()).putBoolean("stopped", false)
                .putString("status", report.joinToString("\n")).commit()
            if (old != null && old != uri) releaseOwnGrant(context, old, grant)
            schedule(context)
            report
        } catch (error: Exception) {
            CourierDatabase(context).use { it.diagnostic("courier", "selection", CourierClient.errorCode(error)) }
            if (tree(context) != uri) releaseOwnGrant(context, uri, grant)
            throw error
        }
    }

    /** Persisted grants are per URI, so a tree the phone vault also uses must keep its grant (W5-A6 / I6). */
    private fun releaseOwnGrant(context: Context, uri: Uri, grant: Int) {
        if (io.github.junhcha.weaver.pipeline.VaultAccess.tree(context) == uri) return
        runCatching { context.contentResolver.releasePersistableUriPermission(uri, grant) }
    }

    fun folder(context: Context): SafCourierFolder {
        check(!prefs(context).getBoolean("stopped", false)) { "Courier stopped; reselect folder" }
        val uri = tree(context) ?: error("Courier folder not selected")
        return SafCourierFolder(context.contentResolver, uri).also { it.checkPermission() }
    }

    fun stop(context: Context) {
        prefs(context).edit().putBoolean("stopped", true)
            .putString("status", "Courier 접근 실패: 작업 중지. 폴더를 다시 선택하세요.").commit()
        io.github.junhcha.weaver.widget.WidgetUpdates.request(context)
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel("courier", "Courier", NotificationManager.IMPORTANCE_DEFAULT))
        if (Build.VERSION.SDK_INT < 33 || ContextCompat.checkSelfPermission(context,
                AndroidManifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) {
            val intent = PendingIntent.getActivity(context, 29, Intent(context, MainActivity::class.java),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
            manager.notify(29, NotificationCompat.Builder(context, "courier")
                .setSmallIcon(R.drawable.ic_launcher_foreground).setContentTitle("Courier 폴더 접근 실패")
                .setContentText("앱에서 동기화 폴더를 다시 선택하세요").setContentIntent(intent).setAutoCancel(true).build())
        }
    }

    fun testAtomicity(context: Context): String = synchronized(lock) {
        val active = folder(context)
        try { CourierDiagnostics.atomicity(active) }
        catch (error: Exception) {
            stop(context)
            if (error.message == "atomicity_failed") {
                prefs(context).edit().putString("unsafeTree", active.tree.toString()).commit()
            }
            throw error
        }
    }

    /** True when some feature has somewhere to go: a selected Syncthing folder or an HTTP route. */
    fun active(context: Context): Boolean = tree(context) != null || TransportRuntime.settings(context).paired

    /** The Syncthing side of the routed folder; a missing or lost folder stands in as [MissingFolder]. */
    fun files(context: Context): CourierFolder {
        if (tree(context) == null || stopped(context)) return MissingFolder
        return try { folder(context) } catch (_: Exception) { stop(context); MissingFolder }
    }

    /** What every Courier reader and writer uses: each directory lives where its feature's route says. */
    fun routed(context: Context): CourierFolder = TransportRuntime.routed(context, files(context))

    fun schedule(context: Context) {
        if (!active(context)) return
        val work = WorkManager.getInstance(context)
        work.enqueueUniquePeriodicWork("courier-periodic", ExistingPeriodicWorkPolicy.KEEP,
            PeriodicWorkRequestBuilder<CourierWorker>(15, TimeUnit.MINUTES)
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS).build())
        TransportRuntime.schedule(context)
        kick(context)
    }
    fun kick(context: Context) {
        if (!active(context)) return
        // Each trigger gets an independent root: an earlier retry must not block fresh data
        // or a restored folder grant. The runtime lock and durable capture state make overlap safe.
        WorkManager.getInstance(context).enqueue(
            OneTimeWorkRequestBuilder<CourierWorker>()
                .addTag("courier-immediate")
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS).build())
        TransportRuntime.kick(context)
    }

    /** Snapshot first; assign ID and persist mapping before making anything visible to Syncthing. */
    fun prepare(context: Context, original: Uri, kind: CaptureKind, channel: Channel,
                extension: String, occurredAt: String? = null, counterpart: String? = null): Manifest = synchronized(lock) {
        require(extension.matches(Regex("[A-Za-z0-9_-]+")) && extension != "json")
        val staging = File(context.filesDir, "courier-staging").apply { mkdirs() }
        val referenced = CourierDatabase(context).use { db -> db.captures().map { it.stagedPath }.toSet() }
        staging.listFiles()?.filter { it.absolutePath !in referenced &&
            System.currentTimeMillis() - it.lastModified() >= CourierRules.temporaryAge
        }?.forEach { it.delete() }
        val temp = File(staging, ".${UUID.randomUUID()}.tmp")
        try {
            FileOutputStream(temp).use { output ->
                context.contentResolver.openInputStream(original)?.use { it.copyTo(output) }
                    ?: error("original_unreadable")
                output.fd.sync()
            }
            val (sha, size) = temp.inputStream().use(CourierClient::digest)
            val id = "cap_${newUlid()}"
            val staged = File(staging, "$id.$extension")
            check(temp.renameTo(staged))
            val existing = try {
                val manifest = Manifest(1, id, kind, channel, staged.name, sha, size,
                    Instant.now().toString(), occurredAt, Manifest.Hints(counterpart), ManifestCaptureHost.PHONE)
                check(WeaverCodec.encode(manifest).toByteArray().size <= CourierRules.maxJson) { "too_large" }
                CourierDatabase(context).use { it.prepare(original.toString(), manifest, staged) }
            } catch (error: Exception) { staged.delete(); throw error }
            if (existing.stagedPath != staged.absolutePath) staged.delete()
            kick(context)
            existing.manifest
        } finally { temp.delete() }
    }

    fun queueVerdict(context: Context, verdict: Verdict) = synchronized(lock) {
        val directory = File(context.filesDir, "courier-verdicts").apply { mkdirs() }
        cleanQueueTemporaries(directory)
        val final = File(directory, "${verdict.packageId}.r${verdict.revision}.json")
        val bytes = WeaverCodec.encode(verdict).toByteArray()
        check(bytes.size <= CourierRules.maxJson) { "too_large" }
        if (final.exists()) check(final.readBytes().contentEquals(bytes)) { "verdict_already_queued" }
        else {
            val temp = File(directory, ".${UUID.randomUUID()}.tmp")
            try {
                FileOutputStream(temp).use { it.write(bytes); it.fd.sync() }
                check(temp.renameTo(final))
            } finally { temp.delete() }
        }
        io.github.junhcha.weaver.inbox.InboxRuntime.submitted(context, verdict.packageId, verdict.revision)
        kick(context)
    }

    internal fun cleanQueueTemporaries(directory: File) {
        val temporary = Regex("\\.[0-9a-f-]{36}\\.tmp")
        directory.listFiles()?.filter { temporary.matches(it.name) &&
            System.currentTimeMillis() - it.lastModified() >= CourierRules.temporaryAge
        }?.forEach { it.delete() }
    }

    private fun newUlid(): String {
        val bytes = ByteArray(16).also { SecureRandom().nextBytes(it) }
        val timestamp = System.currentTimeMillis()
        repeat(6) { bytes[it] = (timestamp ushr ((5 - it) * 8)).toByte() }
        var value = BigInteger(1, bytes)
        val alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
        return CharArray(26).also { chars ->
            for (i in 25 downTo 0) {
                chars[i] = alphabet[value.and(BigInteger.valueOf(31)).toInt()]
                value = value.shiftRight(5)
            }
        }.concatToString()
    }
}

class CourierWorker(context: Context, params: WorkerParameters) : Worker(context, params) {
    /** No network constraint: Syncthing work and spooling for HTTP features never need a connection. */
    override fun doWork(): Result = run(applicationContext, network = false)

    companion object {
        /** One locked pass. With [network] the HTTP exchange talks to the server; otherwise it only spools locally. */
        internal fun run(context: Context, network: Boolean): Result = synchronized(CourierRuntime.lock) {
            if (!CourierRuntime.active(context)) return@synchronized Result.success()
            val settings = TransportRuntime.settings(context)
            // The server decides the routes: read them first (when the network is allowed), then move any unsent items a
            // switch left behind, then run the pass.
            val refresh = if (network) TransportRuntime.refresh(context) else null
            if (CourierRuntime.stopped(context) && !settings.anyExchange) return@synchronized Result.success()
            TransportRuntime.migrateOrphans(context, CourierRuntime.files(context))
            val folder = CourierRuntime.routed(context)
            val exchange = if (network) TransportRuntime.exchange(context) else null
            CourierDatabase(context).use { db ->
                try {
                    val ok = synchronize(context, folder, db, exchange)
                    val summary = exchange?.summary?.also { merge(it, refresh) } ?: refresh
                    if (summary != null) TransportRuntime.finished(context, summary)
                    if (ok && summary?.retry != true) Result.success() else Result.retry()
                } catch (_: SecurityException) {
                    CourierRuntime.stop(context); Result.success()
                } catch (error: Exception) {
                    db.diagnostic("courier", "scan", CourierClient.errorCode(error))
                    io.github.junhcha.weaver.widget.WidgetUpdates.scanFinished(context, false)
                    Result.retry()
                }
            }
        }

        private fun merge(into: io.github.junhcha.weaver.transport.ExchangeSummary, other: io.github.junhcha.weaver.transport.ExchangeSummary?) {
            if (other == null) return
            into.retry = into.retry || other.retry; into.unreachable = into.unreachable || other.unreachable
            into.needsRepair = into.needsRepair || other.needsRepair; into.errorCode = into.errorCode ?: other.errorCode
        }

        internal fun synchronize(context: Context, folder: CourierFolder, db: CourierDatabase,
                                 exchange: io.github.junhcha.weaver.transport.HttpExchange? = null): Boolean {
            exchange?.pull()
            val client = CourierClient(folder, db, CourierRuntime.AUTO_CLEANUP)
            // One listing feeds both capture publication and the review/alert inbox. A listing
            // failure throws before either runs, so it is never read as "every file was deleted".
            val inbox = client.scan()
            io.github.junhcha.weaver.pipeline.PipelineRuntime.observe(context, folder.identity, inbox)
            var successful = client.synchronize(inbox)
            try {
                if (!io.github.junhcha.weaver.inbox.InboxRuntime.apply(context, folder, inbox, db)) successful = false
            } catch (error: SecurityException) { throw error
            } catch (error: Exception) {
                db.diagnostic("proposals", "inbox", CourierClient.errorCode(error)); successful = false
            }
            try {
                if (!io.github.junhcha.weaver.calendar.CalendarRuntime.execute(context, inbox.actions, client, db)) successful = false
            } catch (error: SecurityException) { throw error
            } catch (error: Exception) {
                db.diagnostic("actions", "execute", CourierClient.errorCode(error)); successful = false
            }
            val directory = File(context.filesDir, "courier-verdicts")
            CourierRuntime.cleanQueueTemporaries(directory)
            directory.listFiles()?.filter { it.extension == "json" }?.forEach { file ->
                try {
                    val verdict = file.inputStream().use { CourierClient.decoded<Verdict>(CourierClient.boundedJson(it)) }
                    check(file.name == "${verdict.packageId}.r${verdict.revision}.json") { "id_mismatch" }
                    client.publishVerdict(verdict)
                    check(file.delete()) { "queue_delete_failed" }
                } catch (error: SecurityException) { throw error
                } catch (error: Exception) {
                    db.diagnostic("decisions", file.name, CourierClient.errorCode(error)); successful = false
                }
            }
            try {
                if (!io.github.junhcha.weaver.review.ReviewRuntime.synchronize(context, folder)) successful = false
            } catch (error: SecurityException) { throw error
            } catch (error: Exception) {
                db.diagnostic("decisions", "review", CourierClient.errorCode(error)); successful = false
            }
            if (exchange != null) {
                exchange.push()
                io.github.junhcha.weaver.review.ReviewRuntime.applyOutcomes(context)
                // Receipts the push just wrote flip the capture state now instead of at the next run.
                if (!client.synchronize(client.scan())) successful = false
            }
            try {
                if (!io.github.junhcha.weaver.shepherd.PendingCalls.reconcile(context)) successful = false
            } catch (error: Exception) {
                db.diagnostic("shepherd", "reconcile", CourierClient.errorCode(error))
                successful = false
            }
            io.github.junhcha.weaver.widget.WidgetUpdates.scanFinished(context, successful)
            return successful
        }
    }
}
