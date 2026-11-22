package io.github.junhcha.weaver.transport

import io.github.junhcha.weaver.courier.CourierEntry
import io.github.junhcha.weaver.courier.CourierFolder
import io.github.junhcha.weaver.courier.CourierRules
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.util.UUID

/**
 * App-private copy of the Courier folders for features routed over HTTP. The Courier code writes and reads it
 * exactly like the Syncthing folder (so IDs, file names and rules are shared); the HTTP exchange drains the
 * outgoing folders and fills the incoming ones. Files survive process death and reboot, which is what makes
 * the queue durable: nothing here is deleted before the server confirmed the step (Receipt, `applied`).
 */
class SpoolFolder(private val root: File, override val identity: String = "http-spool") : CourierFolder {
    private val directories = CourierRules.folders + CourierRules.actionFolders + "proposals/pipeline"

    private fun directory(folder: String): File {
        require(folder in directories) { "unknown_folder" }
        return File(root, folder).also { check(it.isDirectory || it.mkdirs()) { "spool_unavailable" } }
    }

    private fun file(folder: String, name: String): File {
        require(CourierRules.safeName(name))
        return File(directory(folder), name)
    }

    override fun list(folder: String): List<CourierEntry> = directory(folder).listFiles().orEmpty()
        .map { CourierEntry(it.name, it.lastModified(), it.isFile) }

    override fun read(folder: String, name: String): InputStream = file(folder, name).inputStream()

    override fun publish(folder: String, name: String, write: (OutputStream) -> Unit) {
        require(!CourierRules.ignored(name))
        val target = file(folder, name)
        check(!target.exists()) { "already_exists" }
        atomically(target, write)
    }

    /** Inbound files (packages, alerts, actions, pipeline items) are replaced when the server version changes. */
    fun put(folder: String, name: String, bytes: ByteArray) {
        require(!CourierRules.ignored(name))
        atomically(file(folder, name)) { it.write(bytes) }
    }

    override fun delete(folder: String, name: String) {
        val target = file(folder, name)
        check(!target.exists() || target.delete()) { "delete_failed" }
    }

    fun exists(folder: String, name: String) = file(folder, name).isFile
    /** The backing file, for streaming a payload without copying it into memory. */
    fun fileOf(folder: String, name: String): File = file(folder, name)

    private fun atomically(target: File, write: (OutputStream) -> Unit) {
        val temp = File(target.parentFile, ".${target.name}.${UUID.randomUUID().toString().replace("-", "").take(16)}.tmp")
        try {
            FileOutputStream(temp).use { out -> write(out); out.flush(); out.fd.sync() }
            check(temp.renameTo(target)) { "rename_failed" }
        } finally { temp.delete() }
    }
}

/** Stands in for an unselected Syncthing folder: its listing proves nothing and nothing can be published to it. */
object MissingFolder : CourierFolder {
    override fun list(folder: String): List<CourierEntry> = emptyList()
    override fun read(folder: String, name: String): InputStream = error("folder_unselected")
    override fun publish(folder: String, name: String, write: (OutputStream) -> Unit) = error("folder_unselected")
    override fun delete(folder: String, name: String) = Unit
    override fun created(folder: String) = true
}

/**
 * One [CourierFolder] over two backends: each Courier directory belongs to a feature, and the feature's route
 * (the server's `capabilities.transports`) decides whether it lives in the Syncthing folder or in the HTTP [spool].
 * `proposals/` mixes review packages and Alert files. Alerts follow their own explicit route; the map
 * follows review only while offered. [identity] is the review route's; [alertSource] independently
 * selects the phone's durable Alert ledger via the fixed SAF identity -> server association.
 */
class RoutedFolder(
    private val files: CourierFolder,
    private val spool: SpoolFolder,
    private val route: (TransportFeature) -> TransportRoute,
    private val offered: (ServerFeature) -> Boolean = { false },
    private val httpIdentity: String = spool.identity,
) : CourierFolder {
    private enum class Kind { PACKAGE, ALERT, OTHER }
    private fun kind(name: String) = when {
        CourierRules.revision.matches(name) -> Kind.PACKAGE
        CourierRules.alert.matches(name) -> Kind.ALERT
        else -> Kind.OTHER
    }

    private fun reviewHttp() = route(TransportFeature.VERDICT) == TransportRoute.HTTP
    private fun alertsHttp() = http(TransportFeature.ALERT) && offered(ServerFeature.ALERTS)
    private fun pipelineHttp() = reviewHttp() && offered(ServerFeature.PIPELINE)
    private fun http(feature: TransportFeature) = route(feature) == TransportRoute.HTTP

    private fun target(folder: String, name: String = ""): CourierFolder = when (folder) {
        "outbox", "receipts" -> if (http(TransportFeature.CAPTURE)) spool else files
        "decisions" -> if (reviewHttp()) spool else files
        CourierRules.ACTIONS, CourierRules.ACTION_RESULTS -> if (http(TransportFeature.ACTION_RESULT)) spool else files
        "proposals/pipeline" -> if (pipelineHttp()) spool else files
        "proposals" -> when (kind(name)) {
            Kind.PACKAGE -> if (reviewHttp()) spool else files
            Kind.ALERT -> if (alertsHttp()) spool else files
            Kind.OTHER -> files
        }
        else -> throw IllegalArgumentException("unknown_folder")
    }

    val alertSource: String get() = if (alertsHttp() || files === MissingFolder) httpIdentity else files.identity
    override val identity: String get() = if (reviewHttp()) httpIdentity else files.identity
    override fun created(folder: String): Boolean =
        if (folder == "proposals") !(reviewHttp() && alertsHttp()) && files.created(folder)
        else target(folder).created(folder)

    override fun list(folder: String): List<CourierEntry> {
        if (folder != "proposals") return target(folder).list(folder)
        val fromFiles = files.list(folder).filter { e -> target(folder, e.name) === files }
        val fromSpool = if (reviewHttp() || alertsHttp()) spool.list(folder).filter { e -> kind(e.name) != Kind.OTHER && target(folder, e.name) === spool } else emptyList()
        return fromFiles + fromSpool
    }
    override fun read(folder: String, name: String) = target(folder, name).read(folder, name)
    override fun publish(folder: String, name: String, write: (OutputStream) -> Unit) = target(folder, name).publish(folder, name, write)
    override fun delete(folder: String, name: String) = target(folder, name).delete(folder, name)
}
