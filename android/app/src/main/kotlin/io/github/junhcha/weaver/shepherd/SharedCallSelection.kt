package io.github.junhcha.weaver.shepherd

import io.github.junhcha.weaver.schema.Channel
import java.text.Normalizer
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.time.format.ResolverStyle
import java.time.temporal.ChronoUnit
import kotlin.math.abs

/** Filename hints preselect the UI only; the user still confirms the channel and call. */
internal object SharedCallSelection {
    /** `통화 <상대>_<yyMMdd>_<HHmmss>_original.txt`, optionally with a ` (n)` duplicate suffix. */
    private val filename =
        Regex("(통화 ([^/\\r\\n]+)_([0-9]{6}_[0-9]{6}))_original(?: \\([1-9][0-9]*\\))?\\.[tT][xX][tT]")
    private val format = DateTimeFormatter.ofPattern("uuuuMMdd_HHmmss").withResolverStyle(ResolverStyle.STRICT)
    /** Recording start and transcript stamp may differ by a few seconds, even across a minute. */
    private const val WINDOW_SECONDS = 60L
    private data class Hint(val stem: String, val counterpart: String, val time: LocalDateTime)

    private fun nfc(value: String): String = Normalizer.normalize(value, Normalizer.Form.NFC)
    private fun person(value: String): String = CallFilename.stripPrefix(nfc(value))

    private fun parse(name: String?): Hint? {
        val match = name?.let { filename.matchEntire(nfc(it)) } ?: return null
        val counterpart = match.groupValues[2]
        if (counterpart.isBlank() || counterpart != counterpart.trim()) return null
        val time = runCatching { LocalDateTime.parse("20${match.groupValues[3]}", format) }.getOrNull() ?: return null
        return Hint(match.groupValues[1], person(counterpart), time)
    }

    fun channel(name: String?, calls: List<PendingCall>): Channel =
        if (parse(name) != null && calls.isNotEmpty()) Channel.CALL else Channel.SHARE_TEXT

    fun call(name: String?, calls: List<PendingCall>): String? {
        val recent = calls.sortedWith(compareByDescending<PendingCall> {
            it.occurredAt?.let { time -> runCatching { OffsetDateTime.parse(time).toInstant() }.getOrNull() }
        }.thenBy { it.id })
        val hint = parse(name) ?: return recent.firstOrNull()?.id
        return recent.mapNotNull { call ->
            // The transcript usually shares the recording's base name, prefix included.
            if (nfc(call.displayName).substringBeforeLast('.') == hint.stem) return@mapNotNull call to 0L
            // Compare local wall times: filenames carry no offset, while occurredAt retains
            // the observation zone. Do not reinterpret old calls in the current device zone.
            val recording = CallFilename.parse(call.displayName, ZoneOffset.UTC)
            val counterpart = (call.counterpart ?: recording.counterpart)?.let(::person)
            val time = (call.occurredAt ?: recording.occurredAt)?.let {
                runCatching { OffsetDateTime.parse(it).toLocalDateTime() }.getOrNull()
            } ?: return@mapNotNull null
            val distance = abs(ChronoUnit.SECONDS.between(time, hint.time))
            if (counterpart == hint.counterpart && distance <= WINDOW_SECONDS) call to distance else null
        }.minByOrNull { it.second }?.first?.id ?: recent.firstOrNull()?.id
    }
}
