package io.github.junhcha.weaver.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.LinkAnnotation
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextLinkStyles
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.withLink
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import io.github.junhcha.weaver.phone.Inline
import io.github.junhcha.weaver.phone.LinkTarget
import io.github.junhcha.weaver.phone.MdBlock
import io.github.junhcha.weaver.phone.PhoneSnapshot
import io.github.junhcha.weaver.phone.WikiPage
import io.github.junhcha.weaver.phone.parseWikiBody
import io.github.junhcha.weaver.phone.resolveLink

@Composable
internal fun WikiList(
    pages: List<WikiPage>, emptyText: String, modifier: Modifier = Modifier.fillMaxSize(), title: String = "Wiki",
    onOpen: (String) -> Unit,
) {
    Column(modifier.testTag("wiki-list")) {
        ScreenTitle(title, "읽기 전용")
        LazyColumn(Modifier.weight(1f), contentPadding = PaddingValues(horizontal = 16.dp)) {
            if (pages.isEmpty()) item { Muted(emptyText) }
            items(pages, key = { it.id }) { page ->
                Column(Modifier.fillMaxWidth().clickable { onOpen(page.id) }.padding(vertical = 10.dp)) {
                    Text(page.title, style = MaterialTheme.typography.bodyLarge)
                    Muted(listOfNotNull(page.kind.label, page.area?.let { "영역: $it" }, "갱신 ${page.updated.shortDate()}").joinToString(" · "))
                }
            }
        }
    }
}

/** Read-only page: frontmatter box, then the Markdown body. People, organizations and areas link to timelines. */
@Composable
internal fun WikiViewer(
    snapshot: PhoneSnapshot,
    page: WikiPage,
    onBack: () -> Unit,
    onLink: (LinkTarget) -> Unit,
    loading: Boolean = false,
    resolve: (String) -> LinkTarget = { resolveLink(snapshot, it) },
) {
    Column(Modifier.fillMaxSize().testTag("wiki-viewer")) {
        Row(Modifier.fillMaxWidth().padding(end = 16.dp), verticalAlignment = Alignment.CenterVertically) {
            TextButton(onClick = onBack, Modifier.defaultMinSize(minWidth = 48.dp, minHeight = 48.dp)) { Text("‹ Wiki") }
            Muted("읽기 전용", Modifier.weight(1f).padding(start = 8.dp))
        }
        Column(
            Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(horizontal = 16.dp, vertical = 4.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(page.title, style = MaterialTheme.typography.titleLarge, color = WeaverTokens.indigo)
            Muted(listOfNotNull(page.kind.label, page.area?.let { "영역: $it" }, "갱신 ${page.updated.shortDate()}").joinToString(" · "))
            Frontmatter(resolve, page, onLink)
            if (loading) Muted("본문을 불러오는 중이에요")
            val blocks = remember(page.body) { parseWikiBody(page.body) }
            blocks.forEach { WikiBlock(resolve, it, onLink) }
        }
    }
}

@Composable
private fun Frontmatter(resolve: (String) -> LinkTarget, page: WikiPage, onLink: (LinkTarget) -> Unit) {
    val fm = page.frontmatter
    val rows = buildList {
        fm.status?.let { add("상태" to listOf<Inline>(Inline.Text(it))) }
        fm.deadline?.let { add("마감" to listOf<Inline>(Inline.Text(it.shortDate()))) }
        if (fm.assignees.isNotEmpty()) add("담당" to fm.assignees.flatMapIndexed { i, name ->
            (if (i > 0) listOf<Inline>(Inline.Text(", ")) else emptyList()) + Inline.Link(name, name)
        })
        fm.next?.let { add("다음" to listOf<Inline>(Inline.Text(it))) }
    }
    if (rows.isEmpty()) return
    Surface(
        Modifier.fillMaxWidth().testTag("wiki-frontmatter"), shape = RoundedCornerShape(WeaverTokens.number("radius.card").dp),
        color = WeaverTokens.surface, border = BorderStroke(1.dp, WeaverTokens.line),
    ) {
        Column(Modifier.padding(horizontal = 10.dp, vertical = 8.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            rows.forEach { (key, value) ->
                Row {
                    Muted(key, Modifier.width(44.dp))
                    Text(inlineText(resolve, value, onLink), style = MaterialTheme.typography.bodyMedium)
                }
            }
        }
    }
}

@Composable
private fun WikiBlock(resolve: (String) -> LinkTarget, block: MdBlock, onLink: (LinkTarget) -> Unit) {
    when (block) {
        is MdBlock.Heading -> Text(
            inlineText(resolve, block.text, onLink), Modifier.padding(top = 10.dp),
            style = when (block.level) {
                1 -> MaterialTheme.typography.titleLarge
                2 -> MaterialTheme.typography.titleLarge.copy(fontSize = 18.sp)
                else -> MaterialTheme.typography.bodyLarge.copy(fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
            },
        )
        is MdBlock.Paragraph -> Text(inlineText(resolve, block.text, onLink), style = MaterialTheme.typography.bodyLarge)
        is MdBlock.Bullet -> Row {
            Text("•", Modifier.padding(end = 6.dp, start = 4.dp), style = MaterialTheme.typography.bodyLarge)
            Text(inlineText(resolve, block.text, onLink), style = MaterialTheme.typography.bodyLarge)
        }
    }
}

/** Links that lead nowhere (no matching person, area or page) stay plain text. */
@Composable
private fun inlineText(resolve: (String) -> LinkTarget, inlines: List<Inline>, onLink: (LinkTarget) -> Unit): AnnotatedString {
    val style = TextLinkStyles(SpanStyle(color = WeaverTokens.indigo, textDecoration = TextDecoration.Underline))
    return buildAnnotatedString {
        inlines.forEach { part ->
            when (part) {
                is Inline.Text -> append(part.text)
                is Inline.Bold -> withStyle(SpanStyle(fontWeight = FontWeight.SemiBold)) { append(part.text) }
                is Inline.Link -> when (val target = resolve(part.target)) {
                    LinkTarget.None -> append(part.label)
                    else -> withLink(LinkAnnotation.Clickable(part.target, style) { onLink(target) }) { append(part.label) }
                }
            }
        }
    }
}
