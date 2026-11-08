package io.github.junhcha.weaver.shepherd

import io.github.junhcha.weaver.schema.Channel
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.time.format.ResolverStyle
import java.time.temporal.ChronoUnit
import kotlin.math.abs

/** Filename hints preselect the UI only; the user still confirms the channel and call. */
internal object SharedCallSelection {
    private val filename = Regex("통화 ([^/\\r\\n]+)_([0-9]{6}_[0-9]{6})_original\\.txt")
    private val format = DateTimeFormatter.ofPattern("uuuuMMdd_HHmmss").withResolverStyle(ResolverStyle.STRICT)
    private data class Hint(val counterpart: String, val time: LocalDateTime)

    private fun parse(name: String?): Hint? {
        val match = name?.let { filename.matchEntire(it) } ?: return null
        val counterpart = match.groupValues[1]
        if (counterpart.isBlank() || counterpart != counterpart.trim()) return null
        val time = runCatching { LocalDateTime.parse("20${match.groupValues[2]}", format) }.getOrNull() ?: return null
        return Hint(counterpart, time)
    }

    fun channel(name: String?, calls: List<PendingCall>): Channel =
        if (parse(name) != null && calls.isNotEmpty()) Channel.CALL else Channel.SHARE_TEXT

    fun call(name: String?, calls: List<PendingCall>): String? {
        val recent = calls.sortedWith(compareByDescending<PendingCall> {
            it.occurredAt?.let { time -> runCatching { OffsetDateTime.parse(time).toInstant() }.getOrNull() }
        }.thenBy { it.id })
        val hint = parse(name) ?: return recent.firstOrNull()?.id
        return recent.mapNotNull { call ->
            // Compare local wall times: filenames carry no offset, while occurredAt retains
            // the observation zone. Do not reinterpret old calls in the current device zone.
            val recording = CallFilename.parse(call.displayName, ZoneOffset.UTC)
            val counterpart = call.counterpart ?: recording.counterpart
            val time = (call.occurredAt ?: recording.occurredAt)?.let {
                runCatching { OffsetDateTime.parse(it).toLocalDateTime() }.getOrNull()
            }
            if (counterpart == hint.counterpart && time != null &&
                time.truncatedTo(ChronoUnit.MINUTES) == hint.time.truncatedTo(ChronoUnit.MINUTES)) {
                call to abs(ChronoUnit.SECONDS.between(time, hint.time))
            } else null
        }.minByOrNull { it.second }?.first?.id ?: recent.firstOrNull()?.id
    }
}
