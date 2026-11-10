package io.github.junhcha.weaver.review

import java.net.URLEncoder

internal data class WikiLink(val start: Int, val end: Int, val label: String, val uri: String?)

internal fun wikiLinks(text: String, vault: String): List<WikiLink> {
    fun encode(value: String) = URLEncoder.encode(value, Charsets.UTF_8.name()).replace("+", "%20")
    return Regex("\\[\\[([^\\[\\]\\n]+)]]").findAll(text).mapNotNull { match ->
        val parts = match.groupValues[1].split('|', limit = 2)
        val name = parts[0].trim()
        if (name.isEmpty()) return@mapNotNull null
        val label = parts.getOrNull(1)?.trim()?.takeIf { it.isNotEmpty() } ?: name
        val uri = vault.trim().takeIf { it.isNotEmpty() }?.let {
            "obsidian://open?vault=${encode(it)}&file=${encode(name)}"
        }
        WikiLink(match.range.first, match.range.last + 1, label, uri)
    }.toList()
}
