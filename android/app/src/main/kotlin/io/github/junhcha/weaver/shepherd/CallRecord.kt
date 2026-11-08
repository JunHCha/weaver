package io.github.junhcha.weaver.shepherd

import java.time.LocalDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.format.ResolverStyle

enum class CallStatus { UNSHARED, PROCESSED }

/**
 * Local ID is an opaque MediaStore URI. Filename-derived values are hints, never entity links.
 * occurredAt uses the first-observation device zone and is an estimate: MediaStore has no
 * recording-time zone, so a zone change while the app is stopped can make the offset inaccurate.
 */
data class CallRecord(
    val id: String,
    val displayName: String,
    val counterpart: String?,
    val occurredAt: String?,
    val status: CallStatus,
)

/** Small synchronous IO boundary for #31. Mark only after the transcript has been durably queued. */
interface ShepherdCalls {
    fun pendingCalls(): List<CallRecord>
    /** True only for the first successful transition; an unknown/already processed ID returns false. */
    fun markProcessed(id: String): Boolean
}

internal data class RecordingMetadata(
    val uri: String, val name: String, val size: Long, val modified: Long, val addedAt: Long,
)
internal data class CallHints(val counterpart: String?, val occurredAt: String?)

internal object CallFilename {
    private val short = Regex("([^/\\r\\n]+)_([0-9]{6}_[0-9]{6})\\.m4a")
    private val long = Regex("Call recording ([^/\\r\\n]+)_([0-9]{8}_[0-9]{6})\\.m4a")
    fun parse(name: String, zone: ZoneId): CallHints {
        val full = long.matchEntire(name)
        val match = full ?: short.matchEntire(name) ?: return CallHints(null, null)
        val counterpart = match.groupValues[1]
        if (counterpart.isBlank() || counterpart != counterpart.trim()) return CallHints(null, null)
        val stamp = if (full == null) "20${match.groupValues[2]}" else match.groupValues[2]
        return try {
            val local = LocalDateTime.parse(stamp,
                DateTimeFormatter.ofPattern("uuuuMMdd_HHmmss").withResolverStyle(ResolverStyle.STRICT))
            val offsets = zone.rules.getValidOffsets(local)
            // Gaps and overlaps cannot establish a unique instant from a filename.
            if (offsets.size != 1) CallHints(null, null)
            else CallHints(counterpart, local.atOffset(offsets.single()).toString())
        } catch (_: java.time.DateTimeException) { CallHints(null, null) }
    }
}

internal object RecordingPaths {
    fun matches(relative: String?, legacy: String?): Boolean = if (relative != null) {
        relative == "Recordings/Call/"
    } else {
        legacy?.matches(Regex("/storage/[^/]+(?:/[^/]+)?/Recordings/Call/[^/]+")) == true
    }
}
