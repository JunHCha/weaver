package io.github.junhcha.weaver.ui

import java.text.Normalizer
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter

private val sentenceEnd = Regex("(?U)…+[\"'”’」』)\\]}]*|[.!?。！？]+[\"'”’」』)\\]}]*(?=\\s|$)|[\\r\\n]+")
private val whitespace = Regex("(?U)\\s+")
private val leadingMarker = Regex("(?U)^(?:\\d+[.)]|[-+*>])(?:\\s+|$)")
private val bareNumber = Regex("^\\d+\\.$")

internal fun displayName(value: String): String = Normalizer.normalize(value, Normalizer.Form.NFC)

/** Keep the first sentence/paragraph; Compose ellipsizes it after two visual lines. */
internal fun threadTitle(content: String): String {
    var text = displayName(content).trim()
    while (true) {
        val marker = leadingMarker.find(text) ?: break
        text = text.substring(marker.range.last + 1).trimStart()
    }
    if (text.isEmpty() || bareNumber.matches(text)) return "내용 없음"
    val end = sentenceEnd.find(text)
    val first = if (end == null) text else text.substring(0, end.range.last + 1)
    return first.trim().replace(whitespace, " ")
}

internal fun sourceTitle(title: String): String = displayName(title).ifBlank { "저장한 항목" }
internal fun wikiPageTitle(path: String): String = displayName(path.substringAfterLast('/').removeSuffix(".md"))

// The wire model has no frontmatter role; root query pages also remain generic Wiki pages.
internal fun wikiPageKind(path: String): String = when (path.substringBefore('/')) {
    "Digest" -> "Digest"
    "summaries" -> "출처 요약"
    else -> "Wiki 페이지"
}

internal fun threadMeta(kind: String, time: String, cardTime: String?): String =
    if (cardTime != null && OffsetDateTime.parse(time).toInstant() == OffsetDateTime.parse(cardTime).toInstant()) kind
    else "$kind · 반영 ${mapTime(time)}"

internal fun mapTime(value: String): String =
    OffsetDateTime.parse(value).atZoneSameInstant(ZoneId.systemDefault())
        .format(DateTimeFormatter.ofPattern("MM-dd HH:mm"))
