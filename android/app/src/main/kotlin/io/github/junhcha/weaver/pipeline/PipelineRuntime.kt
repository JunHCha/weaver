package io.github.junhcha.weaver.pipeline

import android.app.Application
import android.content.Context
import android.util.AtomicFile
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import io.github.junhcha.weaver.courier.*
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.review.ReviewRuntime
import io.github.junhcha.weaver.schema.*
import java.io.File
import java.security.MessageDigest
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

@Serializable
enum class MapNode(val label: String, val subtitle: String) {
    SHEPHERD("Shepherd", "데이터를 모아요."),
    COURIER("Courier", "Mac이 가져가길 기다려요."),
    SPINNER("Spinner", "텍스트를 정리해요."),
    LOOM("Loom", "검토한 내용을 반영해요."),
    USER("You", "검토할 내용을 확인해요."),
    FABRIC("Fabric", "Thread가 시간순으로 쌓여요."),
    WIKI("Wiki", "Fabric을 종합해 페이지와 Digest를 만들어요."),
}

fun PipelineLocation.node(): MapNode = MapNode.valueOf(name)

@Serializable
data class MapItem(
    val id: String,
    val title: String,
    val source: String,
    val node: MapNode,
    val status: String,
    val times: Map<Stage, String>,
    val packageId: String? = null,
    val packageRevision: Long? = null,
    val server: PipelineItem? = null,
) {
    val needsReview: Boolean
        get() = node == MapNode.USER && status == "review"

    val stopped: Boolean
        get() = status == "apply_stopped"

    val stateLabel: String
        get() {
            server?.wikiEdit?.let { edit ->
                return when (edit.state) {
                    "settling" -> if (edit.readyAt != null) "수정 감지 · 편집 안정 대기" else "수정 내용 확인 중"
                    "recorded" -> "Fabric 반영 · Wiki 갱신 중"
                    "published" -> "Wiki 갱신 완료"
                    else -> "추가할 사실 없음 · 편집 이력 보관"
                }
            }
            return when (status) {
                "review" -> "엮은 내용을 확인해 주세요"
                "apply_stopped" -> "반영 ${server?.applyFailureLimit ?: 5}회 실패로 멈췄어요"
                "applying" -> "승인을 보냈어요 · Mac 반영 대기"
                "done" -> if (node == MapNode.WIKI) "Wiki 결과를 만들었어요" else "Fabric에 Thread를 저장했어요"
                "rejected" -> "거절한 항목이에요"
                "waiting" -> "Mac이 가져가길 기다려요 · 할 일이 아니에요"
                "pending" -> "폰에서 보내기 준비 중이에요"
                else -> "${node.label}에서 처리하고 있어요"
            }
        }
}

fun sourceLabel(value: String): String =
    when (value) {
        "wiki_edit" -> "Wiki 직접 수정"
        "call" -> "통화"
        "fleece",
        "voice_memo" -> "음성 메모"
        else -> "공유한 글"
    }

fun mapMovement(previous: MapItem?, current: MapItem): List<MapNode> {
    if (previous == null) return emptyList()
    val server = current.server
    if (server != null && previous.server != null) {
        val path = movement(previous.server, server).map { it.node() }
        return if (previous.node in path) path.dropWhile { it != previous.node } else path
    }
    if (previous.node == current.node) return emptyList()
    if (server != null && previous.server == null)
        return listOf(previous.node) + server.history.map { it.node() }
    return listOf(previous.node, current.node)
}

object PipelineRuntime {
    private fun directory(context: Context) =
        File(context.filesDir, "exchange/pipeline").apply { mkdirs() }

    private fun token(identity: String) =
        MessageDigest.getInstance("SHA-256").digest(identity.toByteArray()).joinToString("") {
            "%02x".format(it)
        }

    private fun identity(context: Context) = CourierRuntime.tree(context)?.toString().orEmpty()

    fun observe(context: Context, identity: String, inbox: CourierClient.Inbox) {
        PipelineStore(directory(context)).merge(identity, inbox.pipeline)
        CourierDatabase(context).use { db ->
            val manifests = db.captures().associateBy { it.manifest.captureId }
            val prefs =
                context.getSharedPreferences(
                    "pipeline-receipts-${token(identity)}",
                    Context.MODE_PRIVATE,
                )
            val editor = prefs.edit()
            inbox.receipts
                .filter { manifests[it.captureId]?.manifest?.sha256 == it.sha256 }
                .forEach {
                    editor.putString(it.captureId, it.receivedAt)
                    editor.putString("${it.captureId}.source", it.sourceId)
                }
            check(editor.commit())
        }
    }

    /**
     * All IO stays outside composition. Invalid or missing sync files retain previous observations.
     */
    fun snapshot(context: Context): List<MapItem> =
        synchronized(CourierRuntime.lock) {
            val identity = identity(context)
            val prefs =
                context.getSharedPreferences(
                    "pipeline-receipts-${token(identity)}",
                    Context.MODE_PRIVATE,
                )
            val sourceCaptures =
                prefs.all
                    .filterKeys { it.endsWith(".source") }
                    .entries
                    .associate { it.value.toString() to it.key.removeSuffix(".source") }
            val records = ReviewRuntime.records(context)
            val submitted = records.map { it.verdict.packageId to it.verdict.revision }.toSet()
            val observed =
                PipelineStore(directory(context))
                    .items(identity)
                    .map { item ->
                        val sent =
                            item.status == PipelineStatus.REVIEW &&
                                (item.packageId to item.packageRevision) in submitted
                        MapItem(
                            item.captureId ?: item.sourceId,
                            item.title.ifBlank { "${sourceLabel(item.sourceType)} 항목" },
                            sourceLabel(item.sourceType),
                            if (sent) MapNode.LOOM else item.location.node(),
                            if (sent) "applying"
                            else
                                when (item.status) {
                                    PipelineStatus.REVIEW -> "review"
                                    PipelineStatus.APPLY_STOPPED -> "apply_stopped"
                                    PipelineStatus.APPLYING -> "applying"
                                    PipelineStatus.DONE -> "done"
                                    PipelineStatus.REJECTED -> "rejected"
                                    else -> "working"
                                },
                            item.stageTimes,
                            item.packageId,
                            item.packageRevision,
                            item,
                        )
                    }
                    .toMutableList()
            // Review packages from older servers remain actionable while they gain the additive
            // observation contract.
            val knownSources = observed.mapNotNull { it.server?.sourceId }.toSet()
            InboxRuntime.reviews(context).forEach { review ->
                val pkg =
                    runCatching { ReviewRuntime.open(context, review.packageId) }.getOrNull()
                        ?: return@forEach
                if (pkg.sourceId in knownSources) return@forEach
                val sent = (pkg.packageId to pkg.revision) in submitted
                observed.add(
                    MapItem(
                        sourceCaptures[pkg.sourceId] ?: pkg.sourceId,
                        pkg.title.ifBlank { "검토할 문서" },
                        sourceLabel(pkg.sourceType),
                        if (sent) MapNode.LOOM else MapNode.USER,
                        if (sent) "applying" else "review",
                        pkg.stageTimes,
                        pkg.packageId,
                        pkg.revision,
                    )
                )
            }
            val captureIds =
                observed.map { it.id }.toSet() +
                    sourceCaptures.filterKeys { it in knownSources }.values
            CourierDatabase(context).use { db ->
                db.captures().forEach { capture ->
                    if (capture.manifest.captureId in captureIds) return@forEach
                    val received = prefs.getString(capture.manifest.captureId, null)
                    // A persisted RECEIVED state is also evidence if the receipt was cleaned by an
                    // older version.
                    val accepted = received != null || capture.state !in listOf("PENDING", "SENT")
                    val times = buildMap {
                        put(Stage.COLLECT, capture.manifest.capturedAt)
                        if (received != null) put(Stage.SEND, received)
                    }
                    observed.add(
                        MapItem(
                            capture.manifest.captureId,
                            "${sourceLabel(capture.manifest.channel.name.lowercase())} · ${capture.manifest.capturedAt.take(10)}",
                            sourceLabel(capture.manifest.channel.name.lowercase()),
                            if (accepted) MapNode.SPINNER
                            else if (capture.state == "PENDING") MapNode.SHEPHERD
                            else MapNode.COURIER,
                            if (accepted) "working"
                            else if (capture.state == "PENDING") "pending" else "waiting",
                            times,
                        )
                    )
                }
            }
            observed.sortedWith(compareBy<MapItem> { if (it.server?.wikiEdit != null) 0 else 1 }
                .thenByDescending { if (it.server?.wikiEdit != null) it.server.updatedAt else "" }
                .thenBy { it.id })
        }

    fun visit(context: Context, current: List<MapItem>): Map<String, List<MapNode>> {
        val atomic = AtomicFile(File(directory(context), "${token(identity(context))}.seen"))
        val previous =
            runCatching {
                    Json.decodeFromString<List<MapItem>>(
                        atomic.openRead().bufferedReader().use { it.readText() }
                    )
                }
                .getOrDefault(emptyList())
                .associateBy { it.id }
        val paths =
            current
                .associate { it.id to mapMovement(previous[it.id], it) }
                .filterValues { it.size > 1 }
        val output = atomic.startWrite()
        try {
            output.write(Json.encodeToString(current).toByteArray())
            atomic.finishWrite(output)
        } catch (error: Exception) {
            atomic.failWrite(output)
            throw error
        }
        return paths
    }
}

data class PipelineScreenState(
    val items: List<MapItem> = emptyList(),
    val paths: Map<String, List<MapNode>> = emptyMap(),
    val loaded: Boolean = false,
    val error: String? = null,
    val visitId: Int = 0,
)

/** Activity-scoped visit retains its replay across recompositions and configuration changes. */
class PipelineViewModel(application: Application) : AndroidViewModel(application) {
    private val mutable = MutableStateFlow(PipelineScreenState())
    val state = mutable.asStateFlow()
    private val played = mutableSetOf<String>()

    fun claimAnimation(key: String): Boolean = synchronized(played) { played.add(key) }

    private val mutex = Mutex()
    @Volatile private var needsVisit = true

    fun endVisit() {
        needsVisit = true
    }

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch(Dispatchers.IO) {
            mutex.withLock {
                runCatching {
                        val items = PipelineRuntime.snapshot(getApplication())
                        val entering = needsVisit
                        val paths =
                            if (entering) PipelineRuntime.visit(getApplication(), items)
                            else mutable.value.paths
                        if (entering) synchronized(played) { played.clear() }
                        needsVisit = false
                        PipelineScreenState(
                            items,
                            paths,
                            true,
                            visitId = mutable.value.visitId + if (entering) 1 else 0,
                        )
                    }
                    .onSuccess { mutable.value = it }
                    .onFailure {
                        mutable.value =
                            mutable.value.copy(
                                loaded = true,
                                error = "흐름을 읽지 못했어요. 동기화 설정을 확인해 주세요.",
                            )
                    }
            }
        }
    }
}
