package io.github.junhcha.weaver.transport

import java.io.File
import java.io.FileOutputStream
import java.util.UUID
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/**
 * What the HTTP exchange remembers between runs, in one small file written atomically: server operation IDs
 * for accepted verdicts and action results, alerts already handed to the inbox, package ETags and per-item
 * blocks. It holds IDs and stable codes only, never content or tokens.
 */
@Serializable
data class TransportData(
    /** Verdict file name -> operation ID once the server answered 202. */
    val verdictOps: Map<String, String> = emptyMap(),
    /** Action ID -> operation ID once the server accepted the result. */
    val resultOps: Map<String, String> = emptyMap(),
    /** `<package_id>.r<revision>` -> ETag the package arrived with (the Verdict's If-Match). */
    val etags: Map<String, String> = emptyMap(),
    /** Alert IDs written to the inbox spool; once the user dismissed one, the server gets its ack. */
    val alertsSeen: Set<String> = emptySet(),
    /** Action IDs whose result the server applied; never downloaded or executed again. */
    val actionsDone: Set<String> = emptySet(),
    /** Item key -> stable error code for items that need the user (conflict, stale, schema). Retried only on request. */
    val blocked: Map<String, String> = emptyMap(),
    /** Verdict file name -> final outcome the review store shows (`APPLIED`, `STALE`, `CONFLICT`, `ERROR`). */
    val verdictOutcomes: Map<String, String> = emptyMap(),
)

class TransportState(private val file: File) {
    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }

    fun load(): TransportData = try { json.decodeFromString(file.readText()) } catch (_: Exception) { TransportData() }

    fun update(change: (TransportData) -> TransportData): TransportData {
        val next = change(load())
        file.parentFile?.mkdirs()
        val temp = File(file.parentFile, ".${UUID.randomUUID()}.tmp")
        try {
            FileOutputStream(temp).use { it.write(json.encodeToString(next).toByteArray()); it.fd.sync() }
            check(temp.renameTo(file)) { "state_write_failed" }
        } finally { temp.delete() }
        return next
    }
}
