package io.github.junhcha.weaver.phone

import java.time.Instant
import java.time.LocalDate

/** Thread kinds as the PC knows them (web `Kind`). */
enum class ThreadKind(val label: String, val styleKey: String) {
    REQUEST("요청", "request"),
    DECISION("결정", "decision"),
    SCHEDULE("일정", "schedule"),
    RECORD("기록", "record"),
    TAG_CORRECTION("태그정정", "hollow"),
    REJECTION("거절", "hollow"),
    EXECUTION("실행", "hollow"),
}

enum class Presentation { FACT, HISTORY }

enum class Direction(val label: String) { RECEIVED("받음"), SENT("보냄") }

enum class TagKind(val label: String) {
    PROJECT("프로젝트"), AREA("영역"), PERSON("사람"), ORGANIZATION("조직"), TOPIC("주제");

    val isParty: Boolean get() = this == PERSON || this == ORGANIZATION

    /** Name on the wire (server `TagKind`). */
    val wire: String get() = name.lowercase()

    companion object {
        fun fromWire(wire: String?): TagKind? = entries.firstOrNull { it.wire == wire }
    }
}

data class PhoneTag(val name: String, val kind: TagKind?)

/**
 * Read-only view of a Fabric Thread. Mirrors web `Thread`: [presentation] is the server's explicit fact/history split,
 * the relation fields ([retracts], [resolves], [updates], [executes], [correctionTarget]) point at the target thread.
 * [deadline] is a calendar date in the server timezone and [scheduleAt] an instant; both are display values, not wire format.
 */
data class PhoneThread(
    val id: String,
    val seq: Long,
    val kind: ThreadKind,
    val content: String,
    val occurredAt: Instant,
    val recordedAt: Instant,
    /** How the thread reached Fabric: 통화, 메모, … */
    val source: String,
    val counterpart: String? = null,
    val tags: List<String> = emptyList(),
    val direction: Direction? = null,
    val deadline: LocalDate? = null,
    val scheduleAt: Instant? = null,
    val field: String? = null,
    val presentation: Presentation? = null,
    val resolves: String? = null,
    val retracts: String? = null,
    val updates: String? = null,
    val executes: String? = null,
    val correction: TagCorrection? = null,
    /**
     * Server-computed values (`/read/threads`): when [open] is non-null the whole thread list comes from the server with
     * effective values already applied, and the phone uses [open], [retracted] and [mentions] as given instead of
     * recomputing them from the history rows.
     */
    val open: Boolean? = null,
    val retracted: Boolean? = null,
    val mentions: List<String>? = null,
    val amends: String? = null,
    val revision: Long = seq,
)

/** A 태그정정 row: [target] gets [add] tags and loses [remove] tags (web `correction`). */
data class TagCorrection(val target: String, val add: List<String> = emptyList(), val remove: List<String> = emptyList())

data class WikiFrontmatter(
    val status: String? = null,
    val deadline: LocalDate? = null,
    val assignees: List<String> = emptyList(),
    val next: String? = null,
)

/** [body] is Markdown; `[[이름]]` links point at people, organizations, areas or other pages. */
data class WikiPage(
    val id: String,
    val title: String,
    val kind: TagKind,
    val area: String? = null,
    val updated: LocalDate,
    val frontmatter: WikiFrontmatter = WikiFrontmatter(),
    val body: String,
    /** SHA-256 of the whole file as the server lists it; the body is fetched when the page is opened. */
    val sha256: String? = null,
)

data class PhoneSnapshot(
    val threads: List<PhoneThread>,
    val tags: List<PhoneTag>,
    val wikiPages: List<WikiPage>,
)

/**
 * Everything the phone screens read. Request handling is exposed separately through [nowController], gated by
 * server capability and the paired token's edit scope. General free-form fact creation is outside this interface.
 */
interface PhoneReadModel {
    /** True for made-up data; the UI then shows a persistent "예시 데이터" badge. */
    val isExample: Boolean get() = false

    /** Non-null when the last [snapshot] came from the on-disk copy because the server could not be reached. */
    val offlineSince: java.time.Instant? get() = null

    val nowController: NowController? get() = null

    suspend fun snapshot(): PhoneSnapshot

    /** Body of a Wiki page the list showed without one (cached, so it also opens offline); null when unavailable. */
    suspend fun wikiBody(page: WikiPage): String? = page.body.takeIf { it.isNotEmpty() }
}

/** Nothing connected: no pairing yet, so a release build has no data to show. */
object EmptyPhoneReadModel : PhoneReadModel {
    override suspend fun snapshot() = PhoneSnapshot(emptyList(), emptyList(), emptyList())
}

val PhoneSnapshot.isEmpty: Boolean get() = threads.isEmpty() && tags.isEmpty() && wikiPages.isEmpty()

/** Release builds never see synthetic data; debug builds may, always badged. A paired phone reads the server. */
object PhoneReadModels {
    fun forBuild(debug: Boolean, example: () -> PhoneReadModel = { SyntheticPhoneReadModel() }): PhoneReadModel =
        if (debug) example() else EmptyPhoneReadModel

    /** Re-decided on every read, so pairing or unpairing in settings takes effect without restarting the screen. */
    fun forContext(context: android.content.Context, debug: Boolean): PhoneReadModel = object : PhoneReadModel {
        @Volatile private var current: PhoneReadModel = forBuild(debug)
        private val nowStore by lazy { NowStore(context.applicationContext) }
        override var nowController: NowController? = null
            private set
        override val isExample: Boolean get() = current.isExample
        override val offlineSince: java.time.Instant? get() = current.offlineSince
        override suspend fun wikiBody(page: WikiPage): String? = current.wikiBody(page)
        override suspend fun snapshot(): PhoneSnapshot {
            val app = context.applicationContext
            val settings = io.github.junhcha.weaver.transport.TransportRuntime.settings(app)
            current = if (!settings.paired) forBuild(debug) else HttpPhoneReadModel(
                { io.github.junhcha.weaver.transport.TransportRuntime.api(app) },
                java.io.File(io.github.junhcha.weaver.storage.AppStorage.exchangeDir(app), "http/phone-snapshot-${settings.serverId}.json"),
                onProblem = { code -> settings.lastReadError = code },
                onWikiProblem = { code -> settings.lastWikiError = code },
            )
            if (!settings.paired) nowController = null
            else if (nowController?.server != settings.serverId) nowController = NowController(
                settings.serverId!!, { io.github.junhcha.weaver.transport.TransportRuntime.api(app) }, nowStore,
            )
            val result = current.snapshot()
            nowController?.refresh()
            return result
        }
    }
}
