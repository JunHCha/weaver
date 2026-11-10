package io.github.junhcha.weaver.shepherd.autoshare

import io.github.junhcha.weaver.shepherd.autoshare.Selectors.CHOOSER
import io.github.junhcha.weaver.shepherd.autoshare.Selectors.VOICE_NOTE
import io.github.junhcha.weaver.shepherd.autoshare.Selectors.WEAVER

/** Pseudonymous recording used in all tests. */
const val STEM = "통화 홍길동_261110_111103"

class FakeNode(
    private val pkg: String,
    private val idName: String? = null,
    private val label: String? = null,
    private val desc: String? = null,
    override val clickable: Boolean = false,
    override val scrollable: Boolean = false,
    override val visible: Boolean = true,
    /** Reading the text of this node fails the test: script content must never be read. */
    private val secret: Boolean = false,
    private val name: String? = null,
    private val onClick: () -> Unit = {},
    private val onScroll: () -> Boolean = { false },
    kids: List<FakeNode> = emptyList(),
) : UiNode {
    private val kidList = kids
    private var parentNode: FakeNode? = null
    var clicks: MutableList<String>? = null
    init { kidList.forEach { it.parentNode = this } }
    override val packageName get() = pkg
    override val id get() = idName?.let { "$pkg:id/$it" }
    override val text: String? get() {
        if (secret) throw AssertionError("script node text was read: $idName")
        return label
    }
    override val contentDescription get() = desc
    override val children get() = kidList
    override val parent get() = parentNode
    private fun root(): FakeNode = generateSequence(this) { it.parentNode }.last()
    override fun click(): Boolean {
        (root().clicks ?: throw AssertionError("detached node")).add(name ?: idName ?: label ?: desc ?: "?")
        onClick()
        return true
    }
    override fun scroll() = onScroll()
}

/** A tiny simulation of the Voice Recorder, the share sheet and Weaver, expanded or folded. */
class FakeDevice(
    val expanded: Boolean,
    val recordings: List<String> = listOf("통화 김철수_261109_090000", STEM, "통화 이영희_261108_080000"),
    var detail: String? = null,
    var transcribed: MutableSet<String> = mutableSetOf(),
    var transcribeMs: Long = 3_000,
    val pageSize: Int = 10,
    val sheetApps: List<String> = listOf("Gmail", "Weaver", "Drive"),
    val sheetPageSize: Int = 10,
    var autoSubmit: Boolean = true,
) {
    var now = 0L
    val clicks = mutableListOf<String>()
    var scrolls = 0
    var backs = 0
    var foreground = VOICE_NOTE
    var submitted = false
    var unknownDialog = false
    var hijackAfterAssist: String? = null
    private var listStart = 0
    private var sheetStart = 0
    private var assistMenu = false
    private var optionsMenu = false
    private var shareDialog = false
    private var sheet = false
    private var transcribeDoneAt: Long? = null

    fun tick() {
        val done = transcribeDoneAt
        if (done != null && now >= done) { detail?.let { transcribed.add(it) }; transcribeDoneAt = null }
    }

    private fun n(pkg: String = VOICE_NOTE, id: String? = null, text: String? = null, desc: String? = null,
                  clickable: Boolean = false, scrollable: Boolean = false, visible: Boolean = true,
                  secret: Boolean = false, name: String? = null, onClick: () -> Unit = {},
                  onScroll: () -> Boolean = { false }, vararg kids: FakeNode) =
        FakeNode(pkg, id, text, desc, clickable, scrollable, visible, secret, name, onClick, onScroll, kids.toList())

    private fun listPane(): FakeNode {
        val visibleRows = recordings.drop(listStart).take(pageSize)
        val rows = visibleRows.map { stem ->
            n(id = "listRow_content", clickable = true, name = "row:$stem", onClick = { detail = stem; assistMenu = false; optionsMenu = false },
                kids = arrayOf(
                    n(id = "listRow_title", text = stem),
                    n(id = "listRow_play_icon", desc = "재생 $stem.m4a", clickable = true, name = "PLAY"),
                    n(id = "listRow_play_pause_icon", clickable = true, name = "PAUSE"),
                ))
        }
        val visible = expanded || detail == null
        return n(id = "list_pane", visible = visible, kids = arrayOf(
            n(id = "list_toolbar", kids = arrayOf(n(id = "more_options", desc = "옵션 더보기", clickable = true, name = "more_options:list"))),
            n(scrollable = true, onScroll = {
                scrolls++
                if (listStart + pageSize >= recordings.size) false else { listStart += pageSize; true }
            }, kids = rows.toTypedArray()),
        ))
    }

    private fun detailPane(stem: String): FakeNode {
        val body = when {
            stem in transcribed -> n(id = "main_stt", secret = true, kids = arrayOf(
                n(id = "stt_speaker_name", secret = true, text = "화자 1"),
                n(id = "stt_content", secret = true, text = "스크립트 내용")))
            transcribeDoneAt != null -> n(kids = arrayOf(
                n(id = "ai_transcribe_progress_percent_text", text = "27%"),
                n(text = "한국어로 텍스트 변환 중…"),
                n(id = "ai_transcribe_cancel_button", clickable = true, name = "CANCEL")))
            else -> n()
        }
        val toolbar = n(id = "fragment_toolbar", kids = arrayOf(
            n(desc = "상위 메뉴로 이동", clickable = true, name = "UP"),
            n(text = stem),
            n(desc = "텍스트 변환 어시스트, 버튼", clickable = true, name = "assist", onClick = {
                assistMenu = true
                hijackAfterAssist?.let { detail = it }
            }),
            n(desc = "검색", clickable = true, name = "SEARCH"),
            n(id = "more_options", desc = "옵션 더보기", clickable = true, name = "more_options:detail", onClick = { optionsMenu = true }),
        ))
        return n(id = "detail_pane", kids = arrayOf(toolbar, body))
    }

    private fun voiceRoots(): List<FakeNode> {
        val roots = mutableListOf(n(id = "main", kids = arrayOf(listPane()) + listOfNotNull(detail?.let(::detailPane))))
        if (assistMenu) roots += n(id = "popup", kids = arrayOf(
            n(id = "transcribe_menu", text = "텍스트 변환", clickable = true, name = "transcribe", onClick = {
                assistMenu = false; transcribeDoneAt = now + transcribeMs }),
            n(id = "summary_menu", text = "요약", clickable = true, name = "SUMMARY"),
            n(id = "translate_menu", text = "번역", clickable = true, name = "TRANSLATE")))
        if (optionsMenu) roots += n(id = "popup", kids = arrayOf(
            n(id = "menu_item_layout", clickable = true, name = "DELETE-ROW", kids = arrayOf(n(id = "icon_menu_3", desc = "삭제"), n(id = "menu_title", text = "삭제"))),
            n(id = "menu_item_layout", clickable = true, name = "DELETE-TRANSCRIPT",
                kids = arrayOf(n(id = "menu_title", text = "텍스트로 변환된 내용 삭제"))),
            n(id = "menu_item_layout", clickable = true, name = "share", onClick = { optionsMenu = false; shareDialog = true },
                kids = arrayOf(n(id = "menu_title", text = "공유")))))
        if (shareDialog) roots += n(id = "dialog", kids = arrayOf(
            n(id = "alertTitle", text = "공유"),
            n(text = "음성 파일", clickable = true, name = "VOICE_FILE"),
            n(text = "텍스트 파일", clickable = true, name = "text_file", onClick = { shareDialog = false; sheet = true; foreground = CHOOSER }),
            n(text = "음성 및 텍스트 파일", clickable = true, name = "BOTH"),
            n(text = "PDF 파일", clickable = true, name = "PDF")))
        if (unknownDialog) roots += n(id = "dialog", kids = arrayOf(n(id = "alertTitle", text = "권한 요청"),
            n(text = "허용", clickable = true, name = "ALLOW")))
        return roots
    }

    private fun sheetRoot(): FakeNode {
        val apps = sheetApps.drop(sheetStart).take(sheetPageSize).map { app ->
            n(CHOOSER, clickable = true, name = "app:$app", onClick = {
                if (app == "Weaver") { sheet = false; foreground = WEAVER; if (autoSubmit) submitted = true }
            }, kids = arrayOf(n(CHOOSER, id = "text1", text = app)))
        }
        return n(CHOOSER, id = "chooser", kids = arrayOf(
            // A direct-share row that must never be used, even though it names the app.
            n(CHOOSER, id = "item", clickable = true, name = "DIRECT", kids = arrayOf(n(CHOOSER, id = "text1", text = "Weaver"))),
            n(CHOOSER, scrollable = true, onScroll = {
                scrolls++
                if (sheetStart + sheetPageSize >= sheetApps.size) false else { sheetStart += sheetPageSize; true }
            }, kids = apps.toTypedArray())))
    }

    fun screen(): Screen {
        val roots: List<FakeNode> = when (foreground) {
            WEAVER -> listOf(n(WEAVER, kids = arrayOf(n(WEAVER, text = "스크립트 미리보기 비밀", secret = true))))
            CHOOSER -> listOf(sheetRoot())
            VOICE_NOTE -> voiceRoots()
            else -> listOf(n(foreground, kids = arrayOf(n(foreground, text = "공유", clickable = true, name = "FOREIGN-SHARE"))))
        }
        roots.forEach { it.clicks = clicks }
        return Screen(roots, foreground) {
            backs++
            detail = null
            true
        }
    }

    companion object {
        /** Everything the machine is allowed to press. Anything else in `clicks` is a bug. */
        val whitelist = setOf("row:$STEM", "assist", "transcribe", "more_options:detail", "share", "text_file", "app:Weaver")
    }
}

/** Steps the machine against the device in 500 ms ticks until it finishes or [limitMs] passes. */
fun run(device: FakeDevice, machine: AutoShareMachine, limitMs: Long = 120_000, each: (FakeDevice) -> Unit = {}): Progress {
    var result: Progress = Progress.Working
    while (device.now <= limitMs) {
        result = machine.advance(device.screen(), device.now)
        if (result != Progress.Working) return result
        each(device)
        device.tick()
        device.now += 500
    }
    return result
}
