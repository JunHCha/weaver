package io.github.junhcha.weaver.ui

import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter

private val sentenceEnd = Regex("[.!?。！？](?=\\s|$)|[\\r\\n]+")
private val whitespace = Regex("\\s+")

/** Keep the first sentence/paragraph; Compose ellipsizes it after two visual lines. */
internal fun threadTitle(content: String): String {
    val text = content.trim()
    if (text.isEmpty()) return "내용 없음"
    val end = sentenceEnd.find(text)
    val first = if (end == null) text else text.substring(0, end.range.last + 1)
    return first.trim().replace(whitespace, " ")
}

internal fun sourceTitle(title: String): String = title.ifBlank { "저장한 항목" }
internal fun wikiPageTitle(path: String): String = path.substringAfterLast('/').removeSuffix(".md")

// The wire model has no frontmatter role: only describe the known output folder.
internal fun wikiPageKind(path: String): String = when (path.substringBefore('/')) {
    "Digest" -> "Digest"
    "summaries" -> "출처 요약"
    else -> "Wiki 페이지"
}

internal fun mapTime(value: String): String =
    OffsetDateTime.parse(value).atZoneSameInstant(ZoneId.systemDefault())
        .format(DateTimeFormatter.ofPattern("MM-dd HH:mm"))
