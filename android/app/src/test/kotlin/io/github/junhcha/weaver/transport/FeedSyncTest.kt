package io.github.junhcha.weaver.transport

import io.github.junhcha.weaver.pipeline.PipelineCache
import java.io.File
import java.lang.reflect.Proxy
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** The feed client against a scripted server whose bodies come from the shared contract fixtures. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class FeedSyncTest {
    @get:Rule val tmp = TemporaryFolder()
    private lateinit var store: FeedStore
    private lateinit var cache: PipelineCache
    private val api = FakeFeedApi()
    private val refreshed = mutableListOf<String>()
    private val failRefresh = mutableSetOf<String>()
    private val invalid = mutableListOf<String>()

    private val epoch = "ep_00000000000000000000000000000001"
    private fun cursor(seq: Long, pin: String = "728c5870") = "$epoch.$seq.$pin"
    private val sourceId = "src_00000000000000000000000001"
    private val item: JsonObject get() = Fx.json("pipeline.item")
    private fun revision(n: Int, id: String = sourceId) = JsonObject(item + ("revision" to JsonPrimitive(n)) + ("source_id" to JsonPrimitive(id)))
    private val etag: String get() = ContractFixtures["pipeline.item"].responseHeaders.getValue("etag")

    @Before fun setup() {
        store = FeedStore(File(tmp.newFolder("feed"), "feed.json"))
        cache = PipelineCache(tmp.newFolder("cache"))
    }

    private fun entry(seq: Long, kind: String, id: String, op: String = "upsert", revision: Long? = null, body: JsonObject? = null) =
        FeedEntry(seq, kind, id, revision, op, body)

    private fun page(vararg entries: FeedEntry, next: Long = entries.lastOrNull()?.seq ?: 0, more: Boolean = false) =
        FeedEnvelope(epoch, entries.toList(), cursor(next), more)

    private fun sync(vararg kinds: String, cacheOverride: PipelineCache = cache) = FeedSync(
        api, store, cacheOverride, kinds.toSet(),
        refresh = { kind -> refreshed += kind; kind !in failRefresh },
        guard = { _, block -> try { block(); true } catch (_: ApiException) { false } },
        onInvalid = { key, _ -> invalid += key },
        now = { 1000L },
    )

    @Test fun snapshotReplacesThePipelineListRefreshesTheListKindsAndCheckpointsTheEndOfTheFeed() {
        api.list = listOf(item)
        api.feed += page(entry(1, "pipeline", sourceId, revision = 1, body = buildJsonObject { put("etag", etag) }),
            entry(2, "alert", "alt_1"), entry(3, "review", "pkg_a", revision = 1), next = 4)
        assertEquals(FeedOutcome.COMPLETE, sync("review", "action", "alert", "pipeline").run())
        assertEquals(listOf(sourceId), cache.items().map { it.sourceId })
        assertEquals("the listing already holds revision 1, so the hint costs no second request", 0, api.itemCalls.size)
        assertEquals(setOf("review", "action", "alert"), refreshed.toSet())
        assertEquals(cursor(4), store.load().cursor)
        assertEquals(listOf(cursor(4)), api.checkpoints)
        assertEquals(cursor(4), store.load().checkpointed)
        assertEquals(setOf("action", "alert", "pipeline", "review"), api.askedKinds.single())
    }

    @Test fun incrementalHintFetchesWithTheCachedEtagAndKeepsTheBodyOn304() {
        cache.put(sourceId, item, etag, 1)
        store.update { FeedData(cursor = cursor(1), kinds = "pipeline") }
        api.feed += page(entry(2, "pipeline", sourceId, revision = 1, body = buildJsonObject { put("etag", etag) }))
        assertEquals(FeedOutcome.COMPLETE, sync("pipeline").run())
        assertTrue("an unchanged hint is not fetched", api.itemCalls.isEmpty())
        api.feed += page(entry(3, "pipeline", sourceId, revision = 2, body = buildJsonObject { put("etag", "\"new\"") }))
        api.item = null // 304
        assertEquals(FeedOutcome.COMPLETE, sync("pipeline").run())
        assertEquals(listOf(sourceId to etag), api.itemCalls)
        assertEquals(1L, cache.cached(sourceId)!!.revision)
        assertTrue(cache.status().applying.isEmpty())
        api.feed += page(entry(4, "pipeline", sourceId, revision = 2, body = buildJsonObject { put("etag", "\"new\"") }))
        api.item = Versioned(revision(2).toString().toByteArray(), "\"new\"")
        assertEquals(FeedOutcome.COMPLETE, sync("pipeline").run())
        assertEquals(2L, cache.cached(sourceId)!!.revision)
        assertEquals("\"new\"", cache.cached(sourceId)!!.etag)
    }

    @Test fun pipelineTombstoneRemovesTheItemButAlertTombstoneNeverTouchesTheLocalAlert() {
        cache.put(sourceId, item, etag, 1)
        store.update { FeedData(cursor = cursor(1), kinds = "alert,pipeline") }
        api.feed += page(entry(2, "alert", "alt_1", op = "tombstone"), entry(3, "pipeline", sourceId, op = "tombstone"))
        assertEquals(FeedOutcome.COMPLETE, sync("alert", "pipeline").run())
        assertTrue(cache.items().isEmpty())
        assertTrue("an expired Alert body is not a local dismiss and starts no list refresh", refreshed.isEmpty())
    }

    @Test fun anUnknownKindStopsTheCheckpointRightBeforeItAndLaterEntriesAreNotApplied() {
        store.update { FeedData(cursor = cursor(1), kinds = "pipeline") }
        api.feed += page(entry(2, "pipeline", sourceId, "tombstone"), entry(3, "hologram", "h1"),
            entry(4, "pipeline", "src_00000000000000000000000002", "tombstone"), next = 5)
        cache.put("src_00000000000000000000000002", revision(1, "src_00000000000000000000000002"), null, 1)
        assertEquals(FeedOutcome.STOPPED, sync("pipeline").run())
        assertEquals("the cursor stops after the last applied entry, never past the unknown one", cursor(2), store.load().cursor)
        assertEquals(listOf(cursor(2)), api.checkpoints)
        assertEquals("feed_kind_unsupported", store.load().stopped)
        assertNotNull("the entry after the unknown kind was not applied", cache.cached("src_00000000000000000000000002"))
    }

    @Test fun anUnknownFirstEntryKeepsTheOldCursor() {
        store.update { FeedData(cursor = cursor(1), kinds = "pipeline", checkpointed = cursor(1)) }
        api.feed += page(entry(2, "hologram", "h1"), next = 3)
        assertEquals(FeedOutcome.STOPPED, sync("pipeline").run())
        assertEquals(cursor(1), store.load().cursor)
        assertTrue(api.checkpoints.isEmpty())
    }

    @Test fun aFailedListRefreshHoldsTheCursorBeforeItsFirstEntryAndTheOtherKindsStillRan() {
        store.update { FeedData(cursor = cursor(1), kinds = "action,review") }
        failRefresh += "review"
        api.feed += page(entry(2, "action", "act_1"), entry(3, "review", "pkg_a", revision = 1), entry(4, "action", "act_2"))
        assertEquals(FeedOutcome.STOPPED, sync("review", "action").run())
        assertEquals(setOf("review", "action"), refreshed.toSet())
        assertEquals(cursor(2), store.load().cursor)
        assertEquals("feed_refresh_failed", store.load().stopped)
    }

    @Test fun staleCursorTakesANewSnapshotAndReplacesTheListIncludingRemovals() {
        cache.put("src_gone", revision(1, "src_gone"), null, 1)
        store.update { FeedData(cursor = cursor(9), kinds = "pipeline") }
        api.changeErrors += ApiException(410, "cursor_expired", false)
        api.list = listOf(item)
        api.feed += page(entry(1, "pipeline", sourceId, revision = 1), next = 2)
        assertEquals(FeedOutcome.COMPLETE, sync("pipeline").run())
        assertEquals(listOf(sourceId), cache.items().map { it.sourceId })
        assertEquals(listOf<String?>(cursor(9), null), api.askedCursors)
        assertEquals(cursor(2), store.load().cursor)
    }

    @Test fun aListThatChangesWhileItIsReadRestartsFromNothingAndNeverAppliesAPartialList() {
        api.listErrors += ApiException(410, "cursor_expired", false)
        api.list = listOf(item)
        api.feed += page(next = 1)
        assertEquals(FeedOutcome.COMPLETE, sync("pipeline").run())
        assertEquals(2, api.listCalls)
        assertEquals(1, cache.items().size)
    }

    @Test fun aFailedListKeepsTheCacheAndTheOldCursor() {
        cache.put(sourceId, item, etag, 1)
        api.listErrors += ApiException(503, "pipeline_unavailable", true)
        assertEquals(FeedOutcome.STOPPED, sync("pipeline").run())
        assertEquals(1, cache.items().size)
        assertNull(store.load().cursor)
    }

    @Test fun aDifferentKindSetDropsTheCursorBecauseItWasCutForAnotherSet() {
        store.update { FeedData(cursor = cursor(5), kinds = "review") }
        api.feed += page(next = 6)
        sync("review", "alert").run()
        assertEquals("a cursor is never continued with another kind set", listOf<String?>(null), api.askedCursors)
        assertEquals(cursor(6), store.load().cursor)
        assertEquals("alert,review", store.load().kinds)
    }

    @Test fun aKindThatLeftTheRequestSetIsNotAppliedAndNothingIsDeleted() {
        cache.put(sourceId, item, etag, 1)
        store.update { FeedData(cursor = cursor(1), kinds = "alert") }
        // review went back to files: even if an entry arrives, the phone does not process it, and the map stays.
        api.feed += page(entry(2, "review", "pkg_a", "tombstone"), entry(3, "pipeline", sourceId, "tombstone"))
        assertEquals(FeedOutcome.COMPLETE, sync("alert").run())
        assertTrue(refreshed.isEmpty())
        assertEquals(1, cache.items().size)
    }

    @Test fun aFailedCheckpointPutIsRetriedLaterWithoutUndoingTheLocalCommit() {
        store.update { FeedData(cursor = cursor(1), kinds = "alert") }
        api.feed += page(entry(2, "alert", "alt_1"))
        api.checkpointErrors += ApiException(503, "unavailable", true)
        assertEquals(FeedOutcome.COMPLETE, sync("alert").run())
        assertEquals(cursor(2), store.load().cursor)
        assertNull(store.load().checkpointed)
        api.feed += page(next = 2)
        sync("alert").run()
        assertEquals(cursor(2), store.load().checkpointed)
    }

    @Test fun anExpiredCheckpointRestartsTheFeed() {
        store.update { FeedData(cursor = cursor(1), kinds = "alert") }
        api.feed += page(next = 1)
        api.checkpointErrors += ApiException(410, "cursor_expired", false)
        sync("alert").run()
        assertNull(store.load().cursor)
    }

    @Test fun aBadBodyIsIsolatedAndTheOtherItemsStillApply() {
        store.update { FeedData(cursor = cursor(1), kinds = "pipeline") }
        api.feed += page(entry(2, "pipeline", "src_bad", revision = 1), entry(3, "pipeline", sourceId, revision = 1), next = 3)
        api.itemFor = { id -> if (id == "src_bad") Versioned("{\"not\":\"an item\"}".toByteArray(), null) else Versioned(item.toString().toByteArray(), etag) }
        assertEquals(FeedOutcome.COMPLETE, sync("pipeline").run())
        assertEquals(listOf(sourceId), cache.items().map { it.sourceId })
        assertEquals(listOf("pipeline:src_bad"), invalid)
        assertTrue(cache.status().applying.isEmpty())
    }

    @Test fun anItemThatIsGoneByNowIsNotAFailure() {
        store.update { FeedData(cursor = cursor(1), kinds = "pipeline") }
        api.feed += page(entry(2, "pipeline", sourceId, revision = 1))
        api.itemFor = { throw ApiException(404, "pipeline_not_found", false) }
        assertEquals(FeedOutcome.COMPLETE, sync("pipeline").run())
        assertTrue(cache.items().isEmpty() && cache.status().applying.isEmpty())
    }

    @Test fun pagesAreFollowedUntilTheFeedEndsAndEachPageCommits() {
        store.update { FeedData(cursor = cursor(1), kinds = "alert") }
        api.feed += page(entry(2, "alert", "a"), more = true)
        api.feed += page(entry(3, "alert", "b"))
        assertEquals(FeedOutcome.COMPLETE, sync("alert").run())
        assertEquals(listOf<String?>(cursor(1), cursor(2)), api.askedCursors)
        assertEquals(cursor(3), store.load().cursor)
        assertEquals("the list is refreshed once, after the last page", listOf("alert"), refreshed)
    }

    @Test fun aSecondExpiryInARowIsRetryableNotAFailureAndTheNextRunStartsOver() {
        store.update { FeedData(cursor = cursor(9), kinds = "alert") }
        api.changeErrors += ApiException(410, "cursor_expired", false)
        api.changeErrors += ApiException(410, "cursor_expired", false)
        val error = runCatching { sync("alert").run() }.exceptionOrNull() as ApiException
        assertTrue("the restart budget is spent, not the server's refusal", error.retryable)
        assertEquals("cursor_expired", error.code)
        assertNull(store.load().cursor)
        api.feed += page(next = 1)
        assertEquals(FeedOutcome.COMPLETE, sync("alert").run())
        assertEquals(cursor(1), store.load().cursor)
    }

    @Test fun aChangedEpochRestartsFromASnapshotAndThePipelineListIsReplaced() {
        cache.put("src_gone", revision(1, "src_gone"), null, 1)
        store.update { FeedData(cursor = cursor(5), kinds = "pipeline") }
        val other = "ep_00000000000000000000000000000002"
        api.feed += FeedEnvelope(other, emptyList(), "$other.1.728c5870", false)
        api.list = listOf(item)
        api.feed += FeedEnvelope(other, emptyList(), "$other.1.728c5870", false)
        assertEquals(FeedOutcome.COMPLETE, sync("pipeline").run())
        assertEquals(listOf<String?>(cursor(5), null), api.askedCursors)
        assertEquals(listOf(sourceId), cache.items().map { it.sourceId })
        assertEquals("$other.1.728c5870", store.load().cursor)
    }

    @Test fun aFirstPageThatStopsDoesNotRewriteThePipelineListAtEveryRun() {
        api.list = listOf(item)
        api.feed += page(entry(1, "hologram", "h1"), next = 2)
        assertEquals(FeedOutcome.STOPPED, sync("pipeline").run())
        api.feed += page(entry(1, "hologram", "h1"), next = 2)
        assertEquals(FeedOutcome.STOPPED, sync("pipeline").run())
        assertEquals("the list of this snapshot is already in the cache", 1, api.listCalls)
        // A new snapshot (the cursor was dropped) reads the list again.
        store.reset()
        api.feed += page(next = 2)
        sync("pipeline").run()
        assertEquals(2, api.listCalls)
    }

    @Test fun theListEtagsSurviveAReplacementWhenTheBodyIsUnchanged() {
        cache.put(sourceId, item, etag, 1)
        api.list = listOf(item)
        api.feed += page(next = 1)
        sync("pipeline").run()
        assertEquals(etag, cache.cached(sourceId)!!.etag)
    }

    @Test fun aMiddlePageThatStopsKeepsWhatEarlierPagesAppliedAndRefreshesOnce() {
        cache.put(sourceId, item, etag, 1)
        store.update { FeedData(cursor = cursor(1), kinds = "alert,pipeline") }
        api.feed += page(entry(2, "pipeline", sourceId, "tombstone"), more = true)
        api.feed += page(entry(3, "alert", "alt_1"), entry(4, "hologram", "h1"), next = 5)
        assertEquals(FeedOutcome.STOPPED, sync("alert", "pipeline").run())
        assertTrue("page one committed before the stop", cache.items().isEmpty())
        assertEquals("through the last applied entry, before the unknown one", cursor(3), store.load().cursor)
        assertEquals(listOf("alert"), refreshed)
        assertEquals(listOf(cursor(3)), api.checkpoints)
    }

    @Test fun aListRefreshThatFailsAfterLaterPagesPullsTheCursorBackBehindItsFirstEntry() {
        store.update { FeedData(cursor = cursor(1), kinds = "alert,pipeline") }
        failRefresh += "alert"
        api.feed += page(entry(2, "alert", "alt_1"), more = true)
        api.feed += page(entry(3, "pipeline", sourceId, "tombstone"), next = 4)
        assertEquals(FeedOutcome.STOPPED, sync("alert", "pipeline").run())
        assertEquals("the alert entry was not done, so nothing after it is checkpointed", cursor(1), store.load().cursor)
        assertEquals("feed_refresh_failed", store.load().stopped)
    }

    @Test fun aCursorIsNotCommittedPastAnEntryWhoseListRefreshIsStillDue() {
        store.update { FeedData(cursor = cursor(1), kinds = "alert,pipeline") }
        api.feed += page(entry(2, "alert", "alt_1"), more = true)
        api.feed += page(entry(3, "pipeline", sourceId, "tombstone"), more = true)
        api.feed += page(next = 5)
        // Killed while reading the last page: the stored cursor must still be before the alert entry.
        val killed = object : RuntimeException() {}
        api.afterPages = { if (api.askedCursors.size == 3) throw killed }
        assertTrue(runCatching { sync("alert", "pipeline").run() }.exceptionOrNull() === killed)
        assertEquals(cursor(1), store.load().cursor)
    }

    @Test fun aDamagedFeedFileStartsANewSnapshotAndNeverFails() {
        File(tmp.root, "feed/feed.json").writeText("{ not json")
        api.list = listOf(item)
        api.feed += page(next = 2)
        assertEquals(FeedOutcome.COMPLETE, sync("pipeline").run())
        assertEquals(listOf<String?>(null), api.askedCursors)
        assertEquals(cursor(2), store.load().cursor)
    }

    @Test fun anInvalidItemIsReportedOnceEvenAcrossSnapshots() {
        val bad = JsonObject(mapOf("source_id" to JsonPrimitive(sourceId)))
        api.list = listOf(bad)
        api.feed += page(next = 1)
        sync("pipeline").run()
        store.reset()
        api.feed += page(next = 1)
        sync("pipeline").run()
        assertEquals(listOf("pipeline:$sourceId"), invalid)
    }

    @Test fun theFeedStateIsItsOwnFileAndNeverReadsOrWritesTheNowAfterSeqOrTheActionCursor() {
        api.feed += page(next = 1)
        sync("alert").run()
        assertEquals("only the feed's own calls were made", emptyList<String>(), api.other)
    }

    private class FakeFeedApi : MobileApi by (Proxy.newProxyInstance(MobileApi::class.java.classLoader, arrayOf(MobileApi::class.java)) { _, m, _ ->
        error("unexpected ${m.name}") } as MobileApi) {
        val feed = ArrayDeque<FeedEnvelope>()
        val changeErrors = ArrayDeque<ApiException>()
        val checkpointErrors = ArrayDeque<ApiException>()
        val listErrors = ArrayDeque<ApiException>()
        var list: List<JsonObject> = emptyList()
        var listCalls = 0
        var afterPages: () -> Unit = {}
        var item: Versioned? = null
        var itemFor: ((String) -> Versioned?)? = null
        val itemCalls = mutableListOf<Pair<String, String?>>()
        val checkpoints = mutableListOf<String>()
        val askedCursors = mutableListOf<String?>()
        val askedKinds = mutableListOf<Set<String>>()
        val other = mutableListOf<String>()

        override fun changes(cursor: String?, kinds: Set<String>, limit: Int): FeedEnvelope {
            askedCursors += cursor; askedKinds += kinds
            afterPages()
            changeErrors.removeFirstOrNull()?.let { throw it }
            return feed.removeFirstOrNull() ?: error("no scripted page")
        }
        override fun putCheckpoint(cursor: String) {
            checkpointErrors.removeFirstOrNull()?.let { throw it }
            checkpoints += cursor
        }
        override fun readPipeline(cursor: String?, limit: Int): Page {
            listCalls++
            listErrors.removeFirstOrNull()?.let { throw it }
            return Page(list, null)
        }
        override fun readPipelineItem(sourceId: String, etag: String?): Versioned? {
            itemCalls += sourceId to etag
            return itemFor?.invoke(sourceId) ?: item
        }
    }
}
