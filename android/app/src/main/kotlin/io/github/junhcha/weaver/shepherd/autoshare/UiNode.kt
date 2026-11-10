package io.github.junhcha.weaver.shepherd.autoshare

import java.text.Normalizer

/**
 * Minimal view of an accessibility node so the state machine stays free of Android classes.
 * [visible] mirrors `isVisibleToUser`: the folded Voice Recorder keeps list nodes in the tree
 * while the detail screen is open, so every lookup ignores nodes (and subtrees) that are not visible.
 */
interface UiNode {
    val packageName: String?
    /** Full resource id such as `com.sec.android.app.voicenote:id/listRow_title`. */
    val id: String?
    val text: String?
    val contentDescription: String?
    val clickable: Boolean
    val scrollable: Boolean
    val visible: Boolean
    val children: List<UiNode>
    val parent: UiNode?
    fun click(): Boolean
    /** Scroll forward by one page. False when the container cannot scroll further. */
    fun scroll(): Boolean
}

/** All visible windows plus the package of the active one. [goBack] is the system Back action. */
class Screen(
    val roots: List<UiNode>,
    val activePackage: String?,
    val goBack: () -> Boolean = { false },
)

internal fun nfc(value: String?): String? = value?.let { Normalizer.normalize(it, Normalizer.Form.NFC) }

internal fun UiNode.idName(): String? = id?.substringAfterLast(":id/", missingDelimiterValue = "")?.ifEmpty { null }

internal fun UiNode.ancestors(): Sequence<UiNode> = generateSequence(parent) { it.parent }

/** The node itself or the nearest clickable ancestor. */
internal fun UiNode.clickableTarget(): UiNode? = if (clickable) this else ancestors().firstOrNull { it.clickable }
