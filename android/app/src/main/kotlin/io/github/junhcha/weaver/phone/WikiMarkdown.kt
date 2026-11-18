package io.github.junhcha.weaver.phone

/** Inline run of a Wiki line. `[[이름]]` / `[[이름|표시]]` become [Link]; `**굵게**` becomes [Bold]. */
sealed interface Inline {
    data class Text(val text: String) : Inline
    data class Bold(val text: String) : Inline
    data class Link(val target: String, val label: String) : Inline
}

sealed interface MdBlock {
    data class Heading(val level: Int, val text: List<Inline>) : MdBlock
    data class Paragraph(val text: List<Inline>) : MdBlock
    data class Bullet(val text: List<Inline>) : MdBlock
}

private val headingPattern = Regex("^(#{1,6})\\s+(.*)$")
private val inlinePattern = Regex("""\[\[([^\]|]+)(?:\|([^\]]+))?]]|\*\*([^*]+)\*\*""")

fun parseInline(line: String): List<Inline> {
    val out = mutableListOf<Inline>()
    var last = 0
    for (m in inlinePattern.findAll(line)) {
        if (m.range.first > last) out += Inline.Text(line.substring(last, m.range.first))
        val link = m.groupValues[1]
        out += if (link.isNotEmpty()) Inline.Link(link.trim(), m.groupValues[2].ifEmpty { link }.trim())
        else Inline.Bold(m.groupValues[3])
        last = m.range.last + 1
    }
    if (last < line.length) out += Inline.Text(line.substring(last))
    return out
}

/** Read-only subset of Markdown: headings, bullets, paragraphs (consecutive lines join), bold and wiki links. */
fun parseWikiBody(body: String): List<MdBlock> {
    val blocks = mutableListOf<MdBlock>()
    val paragraph = mutableListOf<String>()
    fun flush() {
        if (paragraph.isNotEmpty()) blocks += MdBlock.Paragraph(parseInline(paragraph.joinToString(" ")))
        paragraph.clear()
    }
    for (raw in body.lines()) {
        val line = raw.trim()
        val heading = headingPattern.find(line)
        when {
            line.isEmpty() -> flush()
            heading != null -> { flush(); blocks += MdBlock.Heading(heading.groupValues[1].length, parseInline(heading.groupValues[2])) }
            line.startsWith("- ") || line.startsWith("* ") -> { flush(); blocks += MdBlock.Bullet(parseInline(line.drop(2))) }
            else -> paragraph += line
        }
    }
    flush()
    return blocks
}
