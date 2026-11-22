package io.github.junhcha.weaver.transport

import io.github.junhcha.weaver.pipeline.PipelineCache
import java.io.File
import java.io.IOException
import java.lang.reflect.Proxy
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** How the exchange classifies what goes wrong inside the feed and around it (keys, wires, retries, aborts). */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class FeedGuardTest {
    @get:Rule val tmp = TemporaryFolder()
    private lateinit var spool: SpoolFolder
    private lateinit var state: TransportState
    private lateinit var store: FeedStore
    private lateinit var cache: PipelineCache
    private val api = ScriptedApi()
    private val epoch = "ep_00000000000000000000000000000001"
    private fun cursor(seq: Long) = "$epoch.$seq.728c5870"
    private val sourceId = "src_00000000000000000000000001"
    private val item: JsonObject get() = Fx.json("pipeline.item")
    private val http: (TransportFeature) -> TransportRoute = { TransportRoute.HTTP }

    @Before fun setup() {
        spool = SpoolFolder(tmp.newFolder("spool"))
        state = TransportState(File(tmp.newFolder("state"), "state.json"))
        store = FeedStore(File(tmp.newFolder("feed"), "feed.json"))
        cache = PipelineCache(tmp.newFolder("cache"))
    }

    private fun exchange(requested: Set<String>? = setOf("pipeline"), offered: (ServerFeature) -> Boolean = { false }) =
        HttpExchange(api, spool, state, http, offered, null, requested?.let { FeedSession(store, cache, it) })

    private fun hint(seq: Long, op: String = "upsert", id: String = sourceId, kind: String = "pipeline", revision: Long? = 1) =
        FeedEntry(seq, kind, id, revision, op, null)

    private fun page(vararg entries: FeedEntry) = FeedEnvelope(epoch, entries.toList(), cursor(entries.lastOrNull()?.seq ?: 1), false)

    @Test fun twoExpiriesInARowAreRetriedLaterNeverBlocked() {
        store.update { FeedData(cursor = cursor(3), kinds = "pipeline") }
        api.changes = { _ -> throw ApiException(410, "cursor_expired", false) }
        val summary = exchange().pull()
        assertTrue(summary.retry)
        assertTrue("nothing is blocked for good", state.load().blocked.isEmpty())
        // The next scheduled run takes the snapshot and works.
        api.changes = { _ -> page(hint(1, "tombstone")) }
        api.list = emptyList()
        val next = exchange().pull()
        assertFalse(next.retry)
        assertEquals(cursor(1), store.load().cursor)
    }

    @Test fun theFeedAndTheLegacyPipelineListDoNotBlockEachOther() {
        // A feed item blocked on its own key leaves the legacy list item (same source id) running ...
        store.update { FeedData(cursor = cursor(1), kinds = "pipeline") }
        state.update { it.copy(blocked = mapOf("feed:pipeline:$sourceId" to "invalid_item")) }
        api.changes = { _ -> page(hint(2)) }
        api.legacy = listOf(item)
        exchange(offered = { it == ServerFeature.PIPELINE }).pull()
        assertTrue("the legacy list wrote its file", spool.exists("proposals/pipeline", "$sourceId.json"))
        assertTrue(api.itemCalls.isEmpty())
        // ... and a legacy block does not stop the feed item.
        spool.delete("proposals/pipeline", "$sourceId.json")
        state.update { it.copy(blocked = mapOf("pipeline:$sourceId" to "spool_unavailable")) }
        store.update { FeedData(cursor = cursor(1), kinds = "pipeline") }
        api.item = Versioned(item.toString().toByteArray(), "\"e\"")
        exchange(offered = { false }).pull()
        assertEquals(listOf(sourceId), api.itemCalls)
        assertEquals(listOf(sourceId), cache.items().map { it.sourceId })
    }

    @Test fun aFeatureBlockFromTheFeedIsItsOwnAndReviewKeepsRunning() {
        store.update { FeedData(cursor = cursor(1), kinds = "pipeline") }
        api.changes = { _ -> page(hint(2)) }
        api.itemFailure = ApiException(403, "scope_denied", false)
        api.reviewRefs = emptyList()
        exchange().pull()
        assertEquals(setOf("feed"), state.load().blockedFeatures.keys)
        api.reviewCalls = 0
        exchange(requested = null).pull()
        assertEquals("review is not held back by the feed's block", 1, api.reviewCalls)
    }

    @Test fun aBlockedFeedKeyStopsTheCheckpointInsteadOfReadingAsDone() {
        store.update { FeedData(cursor = cursor(1), kinds = "pipeline", checkpointed = cursor(1)) }
        state.update { it.copy(blocked = mapOf("feed:pipeline:$sourceId" to "invalid_item")) }
        api.changes = { _ -> page(hint(2)) }
        exchange().pull()
        assertEquals(cursor(1), store.load().cursor)
        assertEquals("feed_pipeline_failed", store.load().stopped)
        assertTrue(api.checkpoints.isEmpty())
    }

    @Test fun storageAndIoFailuresAreRetriedAndOnlyTheItsOwnFaultsAreBlocked() {
        store.update { FeedData(cursor = cursor(1), kinds = "pipeline") }
        api.changes = { _ -> page(hint(2)) }
        listOf<Throwable>(IOException("disk"), IllegalStateException("cache_unavailable"), IllegalStateException("feed_state_write_failed")).forEach { failure ->
            api.itemFailure = failure
            val summary = exchange().pull()
            assertTrue(failure.toString(), summary.retry)
            assertTrue(failure.toString(), state.load().blocked.isEmpty())
        }
        api.itemFailure = IllegalStateException("something_else")
        val summary = exchange().pull()
        assertFalse(summary.retry)
        assertEquals("something_else", state.load().blocked["feed:pipeline:$sourceId"])
    }

    @Test fun anUnauthorisedOrUnreachableItemInsideAListAbortsTheRunWithoutBlockingTheListKey() {
        api.reviewRefs = listOf(ReviewRef("pkg_a", 1, "\"e\""))
        listOf<Throwable>(ApiException(401, "invalid_token", false), UnreachableException()).forEach { failure ->
            api.reviewFailure = failure
            api.actionCalls = 0; api.legacyCalls = 0
            val summary = exchange(requested = null, offered = { it == ServerFeature.PIPELINE }).pull()
            assertTrue(failure.toString(), summary.needsRepair || summary.unreachable)
            assertTrue("the outer list key is not blocked", state.load().blocked.isEmpty())
            assertEquals("the run stopped: no later list ran", 0, api.actionCalls + api.legacyCalls)
        }
    }

    @Test fun anUnsentVerdictSurvivesAReviewTombstoneAndASupersededOne() {
        spool.put("decisions", "pkg_a.r1.json", "{}".toByteArray())
        spool.put("decisions", "pkg_b.r2.json", "{}".toByteArray())
        state.update { it.copy(etags = mapOf("pkg_a.r1" to "\"a\"", "pkg_b.r2" to "\"b\"")) }
        store.update { FeedData(cursor = cursor(1), kinds = "review") }
        api.reviewRefs = emptyList() // both left the list: finished, and superseded by a correction
        api.changes = { _ -> page(hint(2, "tombstone", "pkg_a", "review"), hint(3, "tombstone", "pkg_b", "review")) }
        exchange(requested = setOf("review")).pull()
        assertEquals(1, api.reviewCalls)
        assertTrue(spool.exists("decisions", "pkg_a.r1.json") && spool.exists("decisions", "pkg_b.r2.json"))
        assertEquals(setOf("pkg_a.r1", "pkg_b.r2"), state.load().etags.keys)
    }

    private class ScriptedApi : MobileApi by (Proxy.newProxyInstance(MobileApi::class.java.classLoader, arrayOf(MobileApi::class.java)) { _, m, _ ->
        error("unexpected ${m.name}") } as MobileApi) {
        var changes: (String?) -> FeedEnvelope = { error("no feed") }
        var list: List<JsonObject> = emptyList()
        var legacy: List<JsonElement> = emptyList()
        var legacyCalls = 0
        var item: Versioned? = null
        var itemFailure: Throwable? = null
        val itemCalls = mutableListOf<String>()
        val checkpoints = mutableListOf<String>()
        var reviewRefs: List<ReviewRef> = emptyList()
        var reviewFailure: Throwable? = null
        var reviewCalls = 0
        var actionCalls = 0

        override fun changes(cursor: String?, kinds: Set<String>, limit: Int) = changes(cursor)
        override fun putCheckpoint(cursor: String) { checkpoints += cursor }
        override fun readPipeline(cursor: String?, limit: Int) = Page(list, null)
        override fun readPipelineItem(sourceId: String, etag: String?): Versioned? {
            itemCalls += sourceId
            itemFailure?.let { throw it }
            return item
        }
        override fun pipeline(cursor: String?, limit: Int): Page { legacyCalls++; return Page(legacy, null) }
        override fun reviews(): List<ReviewRef> { reviewCalls++; return reviewRefs }
        override fun review(packageId: String, revision: Long): Versioned { reviewFailure?.let { throw it }; return Versioned("{}".toByteArray(), "\"e\"") }
        override fun actions(): List<JsonElement> { actionCalls++; return emptyList() }
    }
}
