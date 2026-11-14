package io.github.junhcha.weaver.courier

import java.io.InputStream
import java.io.OutputStream

/** Provider-neutral boundary; a File API adapter can replace SAF after the Fold gate. */
interface CourierFolder {
    fun list(folder: String): List<CourierEntry>
    fun read(folder: String, name: String): InputStream
    /** Must fail if final name exists; sync before publishing, validate returned name. */
    fun publish(folder: String, name: String, write: (OutputStream) -> Unit)
    fun delete(folder: String, name: String)
    /** Stable identity of the selected root; a change means a different folder was selected. */
    val identity: String get() = ""
    /** True if this instance created [folder] because it was missing (its listing proves nothing). */
    fun created(folder: String): Boolean = false
}

data class CourierEntry(val name: String, val modifiedAt: Long, val regular: Boolean = true)

object CourierRules {
    val folders = listOf("outbox", "receipts", "proposals", "decisions")
    /** Action path: the PC publishes `actions/<action_id>.json`, the phone answers in `action-results/`. */
    const val ACTIONS = "actions"
    const val ACTION_RESULTS = "action-results"
    val actionFolders = listOf(ACTIONS, ACTION_RESULTS)
    const val maxJson = 1 shl 20
    const val temporaryAge = 10 * 60 * 1000L
    const val cleanupWait = 15 * 60 * 1000L
    const val ulid = "[0-7][0-9A-HJKMNP-TV-Z]{25}"
    val capture = Regex("cap_$ulid\\.json")
    /** `<package_id>.r<revision>.json`; package IDs are opaque `[A-Za-z0-9_-]+` strings. */
    val revision = Regex("([A-Za-z0-9_-]+)\\.r([1-9][0-9]*)\\.json")
    val alert = Regex("alert_(alt_$ulid)\\.json")
    fun ignored(name: String) = name.startsWith(".") || name.startsWith("~syncthing~")
    fun conflict(name: String) = ".sync-conflict-" in name
    fun safeName(name: String) = name.isNotEmpty() && name !in listOf(".", "..") &&
        name.none { it == '/' || it == '\\' || it.isISOControl() }
    fun phoneTemporary(folder: String, name: String): Boolean = ownTemporary(name) && (
        (folder == "outbox" && name.startsWith(".cap_")) ||
        folder == "decisions" || folder == ACTION_RESULTS || name.startsWith(".probe-")
    )
    fun ownTemporary(name: String) = Regex(
        "\\.(?:cap_$ulid\\.[A-Za-z0-9_-]+|[A-Za-z0-9_-]+\\.r[1-9][0-9]*\\.json|act_[0-9a-f]{32}\\.json|probe-[0-9a-f-]{36}\\.(?:txt|bin))\\.[0-9a-f]{16}\\.tmp",
    ).matches(name)
}
