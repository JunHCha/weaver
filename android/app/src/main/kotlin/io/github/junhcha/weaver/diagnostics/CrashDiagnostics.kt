package io.github.junhcha.weaver.diagnostics

import android.app.Application
import android.content.Context
import android.os.Process
import io.github.junhcha.weaver.BuildConfig
import io.github.junhcha.weaver.transport.MobileApi
import io.github.junhcha.weaver.transport.TransportRuntime
import java.io.File
import java.time.Instant
import java.util.Collections
import java.util.IdentityHashMap
import java.util.UUID
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

@Serializable
data class CrashFrame(val class_name: String, val method_name: String, val file_name: String?, val line_number: Int)
@Serializable
data class CrashException(val exception_class: String, val frames: List<CrashFrame>)
@Serializable
data class CrashScreen(val last_tab: String = "NOW", val map_open: Boolean = false, val selected_node: String? = null, val settings_open: Boolean = false)
@Serializable
data class CrashReport(
    val client_id: String, val occurred_at: String, val app_version: String, val app_build: Int,
    val thread: String, val exceptions: List<CrashException>, val screen: CrashScreen,
)

/** App-private, excluded from backup; only whitelisted fields, never Throwable.toString/printStackTrace/message. */
class CrashStore(private val root: File) {
    private val json = Json { encodeDefaults = true }
    @Synchronized fun capture(thread: Thread, error: Throwable, screen: CrashScreen,
                              id: String = UUID.randomUUID().toString(), at: String = Instant.now().toString()): CrashReport {
        val seen = Collections.newSetFromMap(IdentityHashMap<Throwable, Boolean>())
        val exceptions = mutableListOf<CrashException>()
        fun visit(value: Throwable) {
            if (!seen.add(value)) return
            exceptions += CrashException(value.javaClass.name, value.stackTrace.map {
                CrashFrame(it.className, it.methodName, it.fileName, it.lineNumber)
            })
            value.cause?.let(::visit)
            value.suppressed.forEach(::visit)
        }
        visit(error)
        val report = CrashReport(id, at, BuildConfig.VERSION_NAME, BuildConfig.VERSION_CODE, thread.name, exceptions, screen)
        save(report)
        return report
    }

    @Synchronized fun save(report: CrashReport) {
        require(report.client_id.matches(Regex("[A-Za-z0-9_-]{8,64}")))
        root.mkdirs()
        val temporary = File.createTempFile(".pending-", ".tmp", root)
        java.io.FileOutputStream(temporary).use {
            it.write(json.encodeToString(CrashReport.serializer(), report).toByteArray())
            it.fd.sync()
        }
        check(temporary.renameTo(File(root, "${report.client_id}.json")))
        pending().drop(5).forEach { File(root, "${it.client_id}.json").delete() }
        val summary = File.createTempFile(".summary-", ".tmp", root)
        summary.writeText("${report.occurred_at} · ${report.exceptions.first().exception_class} · ${report.screen.last_tab}" +
            if (report.screen.map_open) " · 지도 ${report.screen.selected_node.orEmpty()}" else "")
        check(summary.renameTo(File(root, "last-summary")))
    }

    fun lastSummary(): String? = File(root, "last-summary").takeIf { it.isFile }?.readText()

    @Synchronized fun pending(): List<CrashReport> = root.listFiles().orEmpty()
        .filter { it.extension == "json" }.mapNotNull {
            runCatching { json.decodeFromString(CrashReport.serializer(), it.readText()) }.getOrNull()
        }.sortedByDescending { Instant.parse(it.occurred_at) }

    /** Receipt must confirm this ID and stored state; exceptions leave the original bytes for the next run. */
    @Synchronized fun send(api: MobileApi) {
        pending().forEach { report ->
            val file = File(root, "${report.client_id}.json")
            api.postCrash(file.readBytes(), report.client_id)
            if (file.exists()) check(file.delete())
        }
    }
}

/** Persists synchronously and always delegates to Android's original fatal handler. */
class CrashHandler(private val store: CrashStore, private val screen: () -> CrashScreen,
                   private val previous: Thread.UncaughtExceptionHandler?) : Thread.UncaughtExceptionHandler {
    override fun uncaughtException(thread: Thread, error: Throwable) {
        try { store.capture(thread, error, screen()) } catch (_: Throwable) { /* even disk failure must terminate normally */ }
        finally {
            if (previous != null) previous.uncaughtException(thread, error)
            else { Process.killProcess(Process.myPid()); kotlin.system.exitProcess(10) }
        }
    }
}

object CrashDiagnostics {
    @Volatile var screen = CrashScreen()
    fun store(context: Context) = CrashStore(File(context.noBackupFilesDir, "crashes"))
    fun send(context: Context) { TransportRuntime.api(context)?.let { store(context).send(it) } }
}

class WeaverApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        Thread.setDefaultUncaughtExceptionHandler(CrashHandler(CrashDiagnostics.store(this), { CrashDiagnostics.screen },
            Thread.getDefaultUncaughtExceptionHandler()))
        if (CrashDiagnostics.store(this).pending().isNotEmpty()) TransportRuntime.kick(this)
    }
}
