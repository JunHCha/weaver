package io.github.junhcha.weaver.transport

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.work.ListenableWorker
import androidx.work.testing.WorkManagerTestInitHelper
import io.github.junhcha.weaver.courier.CourierWorker
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.pipeline.PipelineRuntime
import io.github.junhcha.weaver.storage.AppStorage
import java.io.File
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * The feed, the HTTP map cache and the independent routes through the real worker, with no Syncthing folder selected
 * (every test here runs without the Courier SAF permission) against a server that answers only with contract fixtures.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class FeedExchangeTest {
    private lateinit var context: Context
    private lateinit var server: FakeMobileServer
    private val checkpoints = mutableListOf<String>()
    private val previousFactory = TransportRuntime.apiFactory

    @Before fun setup() {
        context = ApplicationProvider.getApplicationContext()
        listOf("transport", "transport-last", "courier").forEach { context.getSharedPreferences(it, Context.MODE_PRIVATE).edit().clear().commit() }
        File(context.filesDir, "courier.sqlite3").delete()
        File(context.filesDir, "inbox.sqlite3").delete()
        AppStorage.exchangeDir(context).deleteRecursively()
        TransportRuntime.secretBox = { ReverseBox() }
        // Real HTTPS for everything except the checkpoint PUT, which is recorded (its wire form has its own contract test).
        TransportRuntime.apiFactory = { pairing ->
            object : MobileApi by HttpsMobileApi(pairing) {
                override fun putCheckpoint(cursor: String) { checkpoints += cursor }
            }
        }
        WorkManagerTestInitHelper.initializeTestWorkManager(context)
        server = FakeMobileServer()
    }

    @After fun stop() {
        TransportRuntime.apiFactory = previousFactory
        server.close()
        assertEquals("the phone sent what the fixtures expect", emptyList<String>(), server.mismatches.toList())
    }

    private fun pair() {
        TransportRuntime.pair(context, FakeMobileServer.code(server))
        TransportRuntime.refresh(context)
    }

    private fun run(): ListenableWorker.Result = CourierWorker.run(context, network = true)

    private val sourceId = "src_00000000000000000000000001"

    @Test fun theMapRouteIsIndependentOfReviewAndFillsTheCacheWithoutAnySyncthingFolder() {
        server.capabilitiesFrom("pipeline.enabled_capabilities")
        server.serve("pipeline.list", "changes_kinds.negotiated_snapshot")
        pair()
        val settings = TransportRuntime.settings(context)
        assertEquals(TransportRoute.FILES, settings.route(TransportFeature.VERDICT))
        assertTrue(settings.pipelineHttp)
        assertEquals(setOf("pipeline"), TransportRuntime.requestedFeedKinds(settings))
        assertEquals(ListenableWorker.Result.success(), run())
        assertEquals(listOf(sourceId), TransportRuntime.pipelineCache(context).items().map { it.sourceId })
        assertTrue("review runs over files: the legacy /pipeline list and /reviews are not used", server.requests.none { it == "GET /mobile/v1/pipeline" || it.endsWith("/reviews") })
        val cursor = TransportRuntime.feedStore(context).load().cursor!!
        assertEquals(listOf(cursor), checkpoints)
        // The next run continues from the cursor and does not fetch again.
        assertEquals(ListenableWorker.Result.success(), run())
        assertEquals(1, server.count("GET /mobile/v1/read/pipeline") - server.count("GET /mobile/v1/read/pipeline/"))
    }

    @Test fun routedAwayKindsAreNotRequestedAndTheServerIsNotAskedForThem() {
        server.capabilitiesFrom("pipeline.enabled_capabilities")
        pair()
        val settings = TransportRuntime.settings(context)
        assertFalse("review is not served on the feed while it runs over files", "review" in settings.feedKinds)
        assertFalse("alert" in TransportRuntime.requestedFeedKinds(settings))
        assertFalse("review" in TransportRuntime.requestedFeedKinds(settings))
    }

    @Test fun anOlderServerWithoutPipelineKeepsTheLegacyBehaviourAndHasNoFeed() {
        server.capabilities(mapOf("capture" to "files", "review" to "http", "action" to "files"))
        server.serve("reviews.list", "reviews.get_revision")
        pair()
        val settings = TransportRuntime.settings(context)
        assertFalse(settings.pipelineHttp)
        assertTrue(TransportRuntime.requestedFeedKinds(settings).isEmpty())
        assertEquals(ListenableWorker.Result.success(), run())
        assertEquals(listOf("pkg_a"), InboxRuntime.reviews(context).map { it.packageId })
        assertTrue(server.requests.none { it.contains("/changes") })
    }

    @Test fun reviewOverHttpStillPullsItsListWhenTheFeedDoesNotCarryReviewAndTheMapStillShowsWithoutSyncthing() {
        // The server serves only fabric/pipeline/wiki on the feed here: review must keep using its own list.
        server.capabilitiesFrom("pipeline.enabled_capabilities")
        server.capabilities(mapOf("review" to "http"))
        server.serve("reviews.list", "reviews.get_revision", "pipeline.list", "pipeline.older_apk_list", "changes_kinds.negotiated_snapshot")
        pair()
        assertEquals(ListenableWorker.Result.success(), run())
        assertEquals(listOf("pkg_a"), InboxRuntime.reviews(context).map { it.packageId })
        assertEquals(listOf(sourceId), TransportRuntime.pipelineCache(context).items().map { it.sourceId })
        // The existing map reads the HTTP spool even though no Syncthing folder was ever selected.
        assertEquals(sourceId, PipelineRuntime.snapshot(context).single { it.server != null }.server!!.sourceId)
    }

    @Test fun theMapIdentityStaysTheSelectedFolderWhileReviewRunsOverFiles() {
        server.capabilitiesFrom("pipeline.enabled_capabilities")
        context.getSharedPreferences("courier", Context.MODE_PRIVATE).edit().putString("tree", "content://test/root").commit()
        pair()
        assertEquals(TransportRoute.FILES, TransportRuntime.settings(context).route(TransportFeature.VERDICT))
        assertEquals("the existing tree identity, so the stored map and its .seen keep their name", "content://test/root", PipelineRuntime.identity(context))
    }

    @Test fun theMapIdentityIsTheSpoolWithPipelineAndReviewOverHttpAndNoFolderPermission() {
        server.capabilitiesFrom("pipeline.enabled_capabilities")
        server.capabilities(mapOf("review" to "http"))
        pair()
        assertNull("no Courier folder was ever selected", io.github.junhcha.weaver.courier.CourierRuntime.tree(context))
        assertTrue(TransportRuntime.settings(context).pipelineHttp)
        val identity = PipelineRuntime.identity(context)
        assertEquals(TransportRuntime.spool(context).identity, identity)
        assertTrue(identity.isNotEmpty())
    }

    @Test fun unpairingKeepsTheFeedPositionTheCachedMapAndTheQueuedItemsAndSendsNothing() {
        server.capabilitiesFrom("pipeline.enabled_capabilities")
        server.serve("pipeline.list", "changes_kinds.negotiated_snapshot")
        pair()
        run()
        val cursor = TransportRuntime.feedStore(context).load().cursor
        val spool = TransportRuntime.spool(context)
        spool.put("decisions", "pkg_a.r1.json", "{}".toByteArray())
        TransportRuntime.state(context).update { it.copy(verdictOps = mapOf("pkg_a.r1.json" to "op_1")) }
        val before = server.requests.size
        TransportRuntime.unpair(context)
        assertNull("unpaired: nothing is exchanged", TransportRuntime.exchange(context))
        assertFalse(TransportRuntime.settings(context).pipelineHttp)
        assertTrue(TransportRuntime.settings(context).feedKinds.isEmpty())
        assertEquals(ListenableWorker.Result.success(), run())
        assertEquals(before, server.requests.size)
        assertEquals(cursor, TransportRuntime.feedStore(context).load().cursor)
        assertEquals(1, TransportRuntime.pipelineCache(context).items().size)
        assertTrue(TransportRuntime.spool(context).exists("decisions", "pkg_a.r1.json"))
        assertEquals("op_1", TransportRuntime.state(context).load().verdictOps["pkg_a.r1.json"])
    }

    @Test fun retryNowTakesANewSnapshotSoBlockedWorkRunsAgain() {
        server.capabilitiesFrom("pipeline.enabled_capabilities")
        server.serve("pipeline.list", "changes_kinds.negotiated_snapshot")
        pair()
        run()
        assertNotNull(TransportRuntime.feedStore(context).load().cursor)
        TransportRuntime.retryBlocked(context)
        assertNull(TransportRuntime.feedStore(context).load().cursor)
        assertEquals("the cached map stays", 1, TransportRuntime.pipelineCache(context).items().size)
    }

    @Test fun anotherServerNeverSharesTheFeedPositionOrTheCache() {
        server.capabilitiesFrom("pipeline.enabled_capabilities")
        server.serve("pipeline.list", "changes_kinds.negotiated_snapshot")
        pair()
        run()
        val other = FakeMobileServer().also { it.serverId = "srv_00000000000000000000000000000002" }
        try {
            other.capabilitiesFrom("pipeline.enabled_capabilities")
            other.capabilities(serverId = "srv_00000000000000000000000000000002")
            TransportRuntime.pair(context, FakeMobileServer.code(other))
            assertNull(TransportRuntime.feedStore(context).load().cursor)
            assertTrue(TransportRuntime.pipelineCache(context).items().isEmpty())
        } finally { other.close() }
    }
}
