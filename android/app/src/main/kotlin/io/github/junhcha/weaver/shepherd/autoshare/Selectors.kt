package io.github.junhcha.weaver.shepherd.autoshare

/**
 * Every screen lookup and every click permission lives here. Ids are tried first, then text or
 * content description; coordinates are never used. Only the targets in [ClickTarget] can be clicked,
 * and [permits] re-checks the node right before the click.
 */
object Selectors {
    const val VOICE_NOTE = "com.sec.android.app.voicenote"
    const val CHOOSER = "com.android.intentresolver"
    const val WEAVER = "io.github.junhcha.weaver"
    val allowedPackages = setOf(VOICE_NOTE, CHOOSER, WEAVER)
    /** Packages whose nodes are inspected. Weaver's own screen (which previews the script) is never scanned. */
    val inspectedPackages = setOf(VOICE_NOTE, CHOOSER)

    const val LIST_TITLE = "listRow_title"
    const val LIST_CONTENT = "listRow_content"
    const val TOOLBAR = "fragment_toolbar"
    const val MORE_OPTIONS = "more_options"
    const val MORE_OPTIONS_DESC = "옵션 더보기"
    const val ASSIST_DESC_PREFIX = "텍스트 변환 어시스트"
    const val TRANSCRIBE_MENU = "transcribe_menu"
    const val TRANSCRIBE_TEXT = "텍스트 변환"
    const val PROGRESS_PERCENT = "ai_transcribe_progress_percent_text"
    const val PROGRESS_CANCEL = "ai_transcribe_cancel_button"
    const val PROGRESS_TEXT = "텍스트 변환 중"
    const val MENU_ITEM = "menu_item_layout"
    const val MENU_TITLE = "menu_title"
    const val SHARE_TEXT = "공유"
    const val ALERT_TITLE = "alertTitle"
    const val TEXT_FILE = "텍스트 파일"
    const val CHOOSER_LABEL_ID = "text1"
    const val CHOOSER_DIRECT_ROW = "item"
    const val DIRECT_SHARE_BADGE = "sem_target_badge"
    private const val DELETE_WORD = "삭제"

    /** Script content nodes: never descended into, never read. */
    private val opaque = setOf("main_stt", "stt_content", "transcript_title", "stt_speaker_name", "stt_start_of_speech")
    /** Never clicked, whatever else matches. */
    private val forbiddenIds = setOf("listRow_play_icon", "listRow_play_pause_icon", "icon_menu_3",
        "ai_transcribe_cancel_button", "summary_menu", "translate_menu")

    /** Visible nodes of the inspected windows; opaque script subtrees are skipped without reading them. */
    fun nodes(screen: Screen): List<UiNode> {
        val out = ArrayList<UiNode>()
        fun walk(node: UiNode) {
            if (!node.visible) return
            out.add(node)
            if (node.idName() in opaque) return
            node.children.forEach(::walk)
        }
        screen.roots.filter { it.packageName in inspectedPackages }.forEach(::walk)
        return out
    }

    private fun UiNode.hasDescendant(name: String, depth: Int = 0): Boolean =
        depth < 6 && children.any { it.idName() == name || it.hasDescendant(name, depth + 1) }
    private fun List<UiNode>.byId(name: String) = filter { it.idName() == name }
    /** Text accessors that refuse to touch script content nodes. */
    private fun UiNode.t(): String? = if (idName() in opaque) null else nfc(text)
    private fun UiNode.d(): String? = if (idName() in opaque) null else nfc(contentDescription)
    private fun UiNode.label(): String? = t() ?: d()

    // ---- state probes (read-only) ----
    enum class Detail { NONE, MATCH, OTHER }

    fun detail(nodes: List<UiNode>, stem: String): Detail {
        val bars = nodes.byId(TOOLBAR)
        if (bars.isEmpty()) return Detail.NONE
        val titles = nodes.filter { n -> bars.any { bar -> n == bar || n.ancestors().any { it == bar } } }
            .mapNotNull { it.t() }.filter { it.isNotEmpty() }
        return when {
            titles.isEmpty() -> Detail.NONE
            nfc(stem) in titles -> Detail.MATCH
            else -> Detail.OTHER
        }
    }

    /** A recording detail screen (player controls) is showing, not just the list's own toolbar. */
    fun hasDetailContent(nodes: List<UiNode>) = nodes.any {
        it.idName() == "main_control_root_view" || it.idName() == "controlbutton_play_start" || it.idName() == "info_max_text"
    }

    fun hasTranscript(nodes: List<UiNode>) = nodes.any { it.idName() == "stt_content" || it.idName() == "main_stt" }

    fun hasProgress(nodes: List<UiNode>) = nodes.any {
        it.idName() == PROGRESS_PERCENT || it.idName() == PROGRESS_CANCEL || it.t()?.contains(PROGRESS_TEXT) == true
    }

    fun listTitles(nodes: List<UiNode>): List<UiNode> = nodes.byId(LIST_TITLE)

    fun dialogTitle(nodes: List<UiNode>): String? = nodes.byId(ALERT_TITLE).firstNotNullOfOrNull { it.t() }

    fun chooserHasApps(nodes: List<UiNode>) =
        nodes.any { it.packageName == CHOOSER && it.idName() == CHOOSER_LABEL_ID }

    fun scrollContainer(nodes: List<UiNode>, anchor: UiNode?): UiNode? =
        anchor?.ancestors()?.firstOrNull { it.scrollable } ?: nodes.firstOrNull { it.scrollable }

    // ---- click targets ----
    class Target(val kind: ClickTarget, val matched: UiNode, val node: UiNode)

    /** The list row is selected through `listRow_content`, never through its play icons. */
    fun row(nodes: List<UiNode>, stem: String): Target? {
        val title = listTitles(nodes).firstOrNull { it.t() == nfc(stem) } ?: return null
        val content = title.ancestors().firstOrNull { it.idName() == LIST_CONTENT } ?: return null
        return Target(ClickTarget.ROW, title, content)
    }

    fun assist(nodes: List<UiNode>): Target? =
        nodes.firstOrNull { it.d()?.startsWith(ASSIST_DESC_PREFIX) == true }
            ?.let { n -> n.clickableTarget()?.let { Target(ClickTarget.ASSIST, n, it) } }

    fun transcribe(nodes: List<UiNode>): Target? {
        val n = nodes.byId(TRANSCRIBE_MENU).firstOrNull() ?: nodes.firstOrNull { it.label() == TRANSCRIBE_TEXT }
        return n?.clickableTarget()?.let { Target(ClickTarget.TRANSCRIBE, n, it) }
    }

    /** The visible more_options inside the detail toolbar; one folded, two (list + detail) expanded. */
    fun moreOptions(nodes: List<UiNode>): Target? {
        val bars = nodes.byId(TOOLBAR)
        val inBar = nodes.firstOrNull { c ->
            (c.idName() == MORE_OPTIONS || c.d() == MORE_OPTIONS_DESC) &&
                c.ancestors().any { a -> bars.any { it == a } }
        } ?: return null
        return inBar.clickableTarget()?.let { Target(ClickTarget.MORE_OPTIONS, inBar, it) }
    }

    /** `menu_title` is not clickable; its parent `menu_item_layout` is. */
    fun shareMenu(nodes: List<UiNode>): Target? {
        val title = nodes.byId(MENU_TITLE).firstOrNull { it.t() == SHARE_TEXT } ?: return null
        val item = title.ancestors().firstOrNull { it.idName() == MENU_ITEM } ?: return null
        return Target(ClickTarget.SHARE, title, item)
    }

    fun textFile(nodes: List<UiNode>): Target? {
        val n = nodes.firstOrNull { it.idName() != ALERT_TITLE && it.label() == TEXT_FILE } ?: return null
        return n.clickableTarget()?.let { Target(ClickTarget.TEXT_FILE, n, it) }
    }

    /** App row in the system share sheet. Direct-share rows (`item`) are never used. */
    fun weaver(nodes: List<UiNode>, label: String): Target? {
        // App rows and direct-share rows are both `item`; only direct-share targets carry a badge.
        val found = nodes.filter { it.packageName == CHOOSER && it.label() == nfc(label) &&
            it.ancestors().none { a -> a.idName() == CHOOSER_DIRECT_ROW && a.hasDescendant(DIRECT_SHARE_BADGE) } }
        val n = found.firstOrNull { it.idName() == CHOOSER_LABEL_ID } ?: found.firstOrNull() ?: return null
        return n.clickableTarget()?.let { Target(ClickTarget.WEAVER, n, it) }
    }

    /**
     * Final gate before any click: the matched node must satisfy its own target's rule, and neither
     * it nor the node actually clicked may be a destructive, media or cancel item.
     */
    fun permits(target: Target): Boolean {
        val m = target.matched
        val clicked = target.node
        if (m.idName() in forbiddenIds || clicked.idName() in forbiddenIds) return false
        if (listOf(m.label(), clicked.label()).any { it?.contains(DELETE_WORD) == true }) return false
        return when (target.kind) {
            ClickTarget.ROW -> m.idName() == LIST_TITLE && clicked.idName() == LIST_CONTENT
            ClickTarget.ASSIST -> m.d()?.startsWith(ASSIST_DESC_PREFIX) == true
            ClickTarget.TRANSCRIBE -> m.idName() == TRANSCRIBE_MENU || m.label() == TRANSCRIBE_TEXT
            ClickTarget.MORE_OPTIONS -> m.idName() == MORE_OPTIONS || m.d() == MORE_OPTIONS_DESC
            ClickTarget.SHARE -> m.idName() == MENU_TITLE && m.t() == SHARE_TEXT && clicked.idName() == MENU_ITEM
            ClickTarget.TEXT_FILE -> m.label() == TEXT_FILE
            ClickTarget.WEAVER -> m.packageName == CHOOSER
            ClickTarget.BACK -> false
        }
    }
}

/** The only things the machine may ever press. */
enum class ClickTarget { ROW, ASSIST, TRANSCRIBE, MORE_OPTIONS, SHARE, TEXT_FILE, WEAVER, BACK }
