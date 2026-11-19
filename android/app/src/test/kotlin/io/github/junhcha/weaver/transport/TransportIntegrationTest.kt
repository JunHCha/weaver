package io.github.junhcha.weaver.transport

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.work.ListenableWorker
import androidx.work.testing.WorkManagerTestInitHelper
import io.github.junhcha.weaver.courier.CourierDatabase
import io.github.junhcha.weaver.courier.CourierWorker
import io.github.junhcha.weaver.courier.MemoryFolder
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.phone.PhoneReadModels
import io.github.junhcha.weaver.review.ReviewRuntime
import io.github.junhcha.weaver.review.ReviewStatus
import io.github.junhcha.weaver.schema.*
import io.github.junhcha.weaver.storage.AppStorage
import java.io.File
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** The whole phone path against a server that answers only with the shared fixtures. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class TransportIntegrationTest {
    @get:Rule val tmp = TemporaryFolder()
    private lateinit var context: Context
    private lateinit var server: FakeMobileServer
    private val extra = mutableListOf<FakeMobileServer>()
    private val id = Fx.captureId
    private val uploadFlow = arrayOf("captures.receipt_before_upload", "captures.payload_staged", "captures.manifest_stored")
    private val captureOnly = mapOf("capture" to "http", "review" to "files", "action" to "files")
    private val allHttp = mapOf("capture" to "http", "review" to "http", "action" to "http")

    @Before fun setup() {
        context = ApplicationProvider.getApplicationContext()
        listOf("transport", "transport-last", "courier").forEach { context.getSharedPreferences(it, Context.MODE_PRIVATE).edit().clear().commit() }
        File(context.filesDir, "courier.sqlite3").delete()
        AppStorage.exchangeDir(context).deleteRecursively()
        TransportRuntime.secretBox = { ReverseBox() }
        WorkManagerTestInitHelper.initializeTestWorkManager(context)
        server = FakeMobileServer()
    }
    @After fun stop() {
        server.close(); extra.forEach { it.close() }
        assertEquals("the phone sent what the fixtures expect", emptyList<String>(), (server.mismatches + extra.flatMap { it.mismatches }).toList())
    }

    /** Pairs and lets the server's capabilities decide the routes, exactly as the worker does. */
    private fun pairFollowing(transports: Map<String, String> = captureOnly, features: Map<String, Boolean> = emptyMap(), on: FakeMobileServer = server) {
        on.capabilities(transports, features)
        TransportRuntime.pair(context, FakeMobileServer.code(on))
        TransportRuntime.refresh(context)
    }

    /** A capture as the phone stores it: the fixture manifest, its payload staged in the app, recorded in the capture log. */
    private fun capture(manifest: Manifest = Fx.manifest): Manifest {
        val staged = File(context.filesDir, "staged-${manifest.captureId}.txt").apply { writeText(Fx.payloadText) }
        CourierDatabase(context).use { it.prepare("content://fixture/${manifest.captureId}", manifest, staged) }
        return manifest
    }

    private fun run(): ListenableWorker.Result = CourierWorker.run(context, network = true)

    // ---- pairing ----

    @Test fun pairingTrustsOnlyTheServerWhoseCertificateChainsToTheCodesCa() {
        val foreign = FakeMobileServer(pki = TestPki.create()).also { extra += it }
        assertThrows(PinMismatchException::class.java) { TransportRuntime.pair(context, FakeMobileServer.code(foreign, pki = TestPki.default)) }
        assertFalse(TransportRuntime.settings(context).paired)
        assertThrows(ApiException::class.java) { TransportRuntime.pair(context, FakeMobileServer.code(server, token = "wvr_" + "9".repeat(43))) }
        assertFalse(TransportRuntime.settings(context).paired)
        assertThrows("the code names another server than the one that answered", PairingException::class.java) {
            TransportRuntime.pair(context, FakeMobileServer.code(server, serverId = "srv_other"))
        }
        assertFalse(TransportRuntime.settings(context).paired)
        val result = TransportRuntime.pair(context, FakeMobileServer.code(server))
        assertEquals(server.serverId, result.serverId)
        assertTrue(TransportRuntime.settings(context).paired)
    }

    @Test fun pairingStartsFromTheServersTransportsAndRefreshFollowsEveryChange() {
        server.capabilities(allHttp)
        TransportRuntime.pair(context, FakeMobileServer.code(server))
        TransportFeature.entries.forEach { assertEquals(TransportRoute.HTTP, TransportRuntime.settings(context).route(it)) }
        server.capabilities(mapOf("capture" to "files", "review" to "http", "action" to "files"))
        TransportRuntime.refresh(context)
        assertEquals(TransportRoute.FILES, TransportRuntime.settings(context).route(TransportFeature.CAPTURE))
        assertEquals(TransportRoute.HTTP, TransportRuntime.settings(context).route(TransportFeature.VERDICT))
        assertFalse("alerts are not offered by this server", TransportRuntime.settings(context).feature(ServerFeature.ALERTS))
    }

    @Test fun revokedNodeShowsRePairAndRetryBlockedDoesNotHideIt() {
        pairFollowing(); capture()
        server.failWith("/receipt", "errors.node_mismatch")
        assertEquals(ListenableWorker.Result.success(), run())
        val settings = TransportRuntime.settings(context)
        assertTrue(settings.needsRepair)
        assertEquals(1, TransportRuntime.queue(context).captures)
        assertNull("no exchange while the node is rejected", TransportRuntime.exchange(context))
        TransportRuntime.retryBlocked(context)
        assertTrue("retry must not clear a real re-pair need", settings.needsRepair)
        server.clearFailures()
        TransportRuntime.pair(context, FakeMobileServer.code(server))
        assertFalse(settings.needsRepair)
    }

    @Test fun aLostKeystoreKeyShowsPairAgainNotConnected() {
        pairFollowing()
        TransportRuntime.secretBox = { LostKeyBox() }
        assertNull(TransportRuntime.api(context))
        assertTrue(TransportRuntime.settings(context).needsRepair)
        assertNull(TransportRuntime.exchange(context))
    }

    // ---- queue: offline, retry, idempotence ----

    @Test fun aCaptureQueuesWhileTheMacIsOffAndArrivesOnceWhenItReturns() {
        pairFollowing(); server.serve(*uploadFlow)
        server.offline = true
        capture()
        assertEquals(ListenableWorker.Result.retry(), run())
        val settings = TransportRuntime.settings(context)
        assertEquals("unreachable", settings.lastError)
        assertEquals(1, TransportRuntime.queue(context).captures)
        server.offline = false
        assertEquals(ListenableWorker.Result.success(), run())
        assertEquals(0, TransportRuntime.queue(context).captures)
        assertNull(settings.lastError)
        assertTrue(settings.lastSyncAt > 0)
        CourierDatabase(context).use { db -> assertEquals("RECEIVED", db.captures().single().state) }
        run()
        assertEquals("a finished capture is not uploaded again", 1, server.count("/payload"))
    }

    @Test fun lostResponsesAcrossRunsNeverDuplicateOrLoseACapture() {
        pairFollowing(); server.serve("captures.receipt_before_upload", "captures.receipt", "captures.payload_staged", "captures.manifest_stored")
        capture()
        server.dropResponse.add("/manifest")
        assertEquals(ListenableWorker.Result.retry(), run())
        assertEquals(ListenableWorker.Result.success(), run())
        assertEquals(1, server.count("/manifest"))
        assertEquals(1, server.count("/payload"))
        CourierDatabase(context).use { db -> assertEquals("RECEIVED", db.captures().single().state) }
    }

    // ---- route changes and unpairing move every queued item with its original ID ----

    private val verdictName = "${Fx.pkg}.r1.json"
    private val resultName = "${Fx.actionId}.json"

    private fun queueEverythingOffline(): Manifest {
        pairFollowing(allHttp)
        server.offline = true
        val manifest = capture()
        run() // the capture reaches the spool, the Mac is off
        val spool = TransportRuntime.spool(context)
        spool.publish("decisions", verdictName) { it.write(WeaverCodec.encode(Fx.verdict).toByteArray()) }
        spool.publish("action-results", resultName) { it.write(Fx.resultText.toByteArray()) }
        val queue = TransportRuntime.queue(context)
        assertEquals(1, queue.captures); assertEquals(1, queue.verdicts); assertEquals(1, queue.results)
        return manifest
    }

    @Test fun whenTheServerSwitchesFeaturesBackToSyncthingEveryUnsentItemMovesWithItsOriginalId() {
        val manifest = queueEverythingOffline()
        server.capabilities(mapOf("capture" to "files", "review" to "files", "action" to "files"))
        server.offline = false
        TransportRuntime.refresh(context)
        val files = MemoryFolder()
        assertEquals(4, TransportRuntime.migrateOrphans(context, files))
        assertEquals(Fx.payloadText, files.files.getValue("outbox/${manifest.payload}").decodeToString())
        assertTrue(files.files.containsKey("outbox/$id.json"))
        assertTrue(files.files.containsKey("decisions/$verdictName"))
        assertTrue(files.files.containsKey("action-results/$resultName"))
        assertTrue("payload goes before its manifest", files.published.indexOf(manifest.payload) < files.published.indexOf("$id.json"))
        assertEquals(QueueCounts(0, 0, 0, 0), TransportRuntime.queue(context))
        assertEquals(0, TransportRuntime.migrateOrphans(context, files))
    }

    @Test fun unpairingMovesQueuedItemsToSyncthingAndLeavesNothingOrphaned() {
        queueEverythingOffline()
        TransportRuntime.unpair(context)
        assertEquals("Syncthing is not selected here, so nothing may be dropped: the items wait in the spool", 3, TransportRuntime.queue(context).total)
        val files = MemoryFolder()
        TransportRuntime.migrateOrphans(context, files)
        assertEquals(0, TransportRuntime.queue(context).total)
        assertTrue(files.files.containsKey("outbox/$id.json"))
        assertEquals(TransportRoute.FILES, TransportRuntime.settings(context).route(TransportFeature.CAPTURE))
    }

    @Test fun nothingMovesWhileTheSyncthingFolderIsUnavailable() {
        queueEverythingOffline()
        server.capabilities(mapOf("capture" to "files", "review" to "files", "action" to "files"))
        server.offline = false
        TransportRuntime.refresh(context)
        assertEquals(0, TransportRuntime.migrateOrphans(context, MissingFolder))
        assertEquals(3, TransportRuntime.queue(context).total)
    }

    @Test fun aSwitchToHttpResendsAnUnconfirmedCaptureOverHttpWithTheSameIdAndBytes() {
        pairFollowing(mapOf("capture" to "files", "review" to "files", "action" to "files"))
        val manifest = capture()
        val files = MemoryFolder()
        val settings = TransportRuntime.settings(context)
        val routed = RoutedFolder(files, TransportRuntime.spool(context), settings::route)
        CourierDatabase(context).use { db -> CourierWorker.synchronize(context, routed, db) }
        assertEquals(Fx.payloadText, files.files.getValue("outbox/${manifest.payload}").decodeToString())
        server.capabilities(captureOnly); server.serve(*uploadFlow)
        TransportRuntime.refresh(context)
        CourierDatabase(context).use { db -> CourierWorker.synchronize(context, routed, db, TransportRuntime.exchange(context)) }
        assertEquals("sent once, with the manifest's own ID", 1, server.count("/manifest"))
        assertTrue(TransportRuntime.spool(context).exists("receipts", "$id.json"))
    }

    @Test fun mixedRoutesKeepCapturesOnSyncthingAndSendOnlyTheChosenFeaturesOverHttp() {
        pairFollowing(mapOf("capture" to "files", "review" to "http", "action" to "files"))
        server.serve("reviews.list", "reviews.get_revision")
        val manifest = capture()
        val files = MemoryFolder()
        val routed = RoutedFolder(files, TransportRuntime.spool(context), TransportRuntime.settings(context)::route)
        CourierDatabase(context).use { db -> CourierWorker.synchronize(context, routed, db) }
        assertTrue("capture stays on the Syncthing folder", files.files.containsKey("outbox/${manifest.captureId}.json"))
        assertTrue(TransportRuntime.spool(context).list("outbox").isEmpty())
        assertTrue(server.requests.none { it.contains("captures") })
    }

    // ---- one spool per server ----

    @Test fun aDifferentServerGetsItsOwnSpoolAndTheOldItemsAreKeptAndWarnedAbout() {
        pairFollowing()
        server.offline = true
        capture()
        run()
        assertEquals(1, TransportRuntime.queue(context).captures)
        val second = FakeMobileServer().also { extra += it }
        second.capabilities(captureOnly, serverId = "srv_second")
        val result = TransportRuntime.pair(context, FakeMobileServer.code(second, serverId = "srv_second"))
        assertEquals("srv_second", result.serverId)
        assertEquals(1, result.otherServerQueued)
        assertEquals("the new server starts with an empty queue", 0, TransportRuntime.queue(context).total)
        assertEquals(1, TransportRuntime.otherServerQueue(context))
        // Identity follows the verdict route; with review on Syncthing it is the folder's, so route review over HTTP to see it.
        second.capabilities(allHttp, serverId = "srv_second"); TransportRuntime.refresh(context)
        assertEquals("http:srv_second", TransportRuntime.routed(context, MissingFolder).identity)
        assertEquals("server A's copy is untouched", 1, TransportRuntime.otherServerQueue(context))
    }

    // ---- review round trip ----

    @Test fun reviewArrivesInTheInboxIsAnsweredOverHttpAndEndsAsApplied() {
        pairFollowing(mapOf("capture" to "files", "review" to "http", "action" to "files"))
        server.serve("reviews.list", "reviews.get_revision", "reviews.verdict_accepted", "reviews.operation_queued")
        assertEquals(ListenableWorker.Result.success(), run())
        assertEquals(listOf(Fx.pkg), InboxRuntime.reviews(context).map { it.packageId })
        val pkg = ReviewRuntime.open(context, Fx.pkg)
        ReviewRuntime.submit(context, pkg, Fx.verdict)
        assertEquals(ListenableWorker.Result.success(), run())
        assertEquals(1, server.count("/verdict"))
        assertEquals("accepted, not yet applied", ReviewStatus.SENT, ReviewRuntime.records(context).single().status)
        server.reset("reviews.operation_queued"); server.serve("reviews.operation_applied")
        run()
        assertEquals(ReviewStatus.APPLIED, ReviewRuntime.records(context).single().status)
        assertEquals(0, TransportRuntime.queue(context).verdicts)
    }

    @Test fun alertsStayOnTheSyncthingFolderWhileTheServerDoesNotOfferThemOverHttp() {
        pairFollowing(mapOf("capture" to "files", "review" to "http", "action" to "files"), features = mapOf("alerts" to false, "pipeline" to false))
        val files = MemoryFolder()
        files.put("proposals", "alert_alt_01ARZ3NDEKTSV4RRFFQ69G5FA9.json", File(SharedSchema.dir, "examples/Alert.json").readText())
        files.put("proposals/pipeline", "src_x.json", "{}")
        val settings = TransportRuntime.settings(context)
        val routed = RoutedFolder(files, TransportRuntime.spool(context), settings::route, settings::feature)
        assertEquals("the alert still comes from Syncthing while review runs over HTTP",
            listOf("alert_alt_01ARZ3NDEKTSV4RRFFQ69G5FA9.json"), routed.list("proposals").map { it.name }.filter { it != "pipeline" })
        assertEquals(listOf("src_x.json"), routed.list("proposals/pipeline").map { it.name })
        server.serve("reviews.list", "reviews.get_revision")
        run()
        assertTrue(server.requests.none { it.contains("/alerts") || it.contains("/pipeline") })
    }

    // ---- read model problems reach the connection card ----

    @Test fun anUnknownThreadKindIsCountedIntoLastErrorNotSilentlyDropped() = runBlocking {
        pairFollowing()
        server.serveDerived("read.threads_first_page") { body ->
            val root = body.jsonObject
            val items = (root["items"] as JsonArray).toMutableList()
            items[0] = JsonObject(items[0].jsonObject.toMutableMap().also { it["kind"] = JsonPrimitive("미지") })
            JsonObject(root.toMutableMap().also { it["items"] = JsonArray(items) })
        }
        server.serve(*Fx.threadPages.drop(1).toTypedArray(), "read.tags", "wiki.list")
        val snapshot = PhoneReadModels.forContext(context, debug = false).snapshot()
        assertEquals(Fx.threadCount - 1, snapshot.threads.size)
        assertEquals("read:unknown_thread_kind:1", TransportRuntime.settings(context).lastError)
        server.reset(*Fx.threadPages.toTypedArray())
        server.serve(*Fx.threadPages.toTypedArray())
        PhoneReadModels.forContext(context, debug = false).snapshot()
        assertNull("a clean read clears the note", TransportRuntime.settings(context).lastError)
    }

    @Test fun readsFromTheUiMoveBlockingNetworkAndWikiRequestsOffTheMainThread() = runBlocking {
        pairFollowing()
        server.serve(*Fx.threadPages.toTypedArray(), "read.tags", "wiki.list", "wiki.page")
        val client = HttpsMobileApi(FakeMobileServer.pairing(server))
        val guarded = object : MobileApi by client {
            private fun requireWorker() {
                if (Thread.currentThread() == android.os.Looper.getMainLooper().thread) {
                    throw android.os.NetworkOnMainThreadException()
                }
            }
            override fun threads(cursor: String?, limit: Int): Page { requireWorker(); return client.threads(cursor, limit) }
            override fun tags(): List<kotlinx.serialization.json.JsonElement> { requireWorker(); return client.tags() }
            override fun wikiPages(cursor: String?, limit: Int): Page { requireWorker(); return client.wikiPages(cursor, limit) }
            override fun wikiPage(id: String, etag: String?): Versioned? { requireWorker(); return client.wikiPage(id, etag) }
        }
        assertEquals(android.os.Looper.getMainLooper().thread, Thread.currentThread())
        val model = io.github.junhcha.weaver.phone.HttpPhoneReadModel({ guarded }, tmp.newFile("read-cache"))
        val snapshot = model.snapshot()
        assertEquals(Fx.threadCount, snapshot.threads.size)
        assertEquals(2, snapshot.wikiPages.size)
        assertTrue(model.wikiBody(snapshot.wikiPages.first { it.title == "방화벽 교체" })!!.contains("노후 장비"))
    }

    @Test fun successfulCourierExchangeCannotClearAReadFailure() = runBlocking {
        pairFollowing()
        server.serve(*Fx.threadPages.toTypedArray(), "read.tags", "wiki.list")
        server.failWith("/read/tags", "errors.scope_denied")
        PhoneReadModels.forContext(context, debug = false).snapshot()
        val settings = TransportRuntime.settings(context)
        assertEquals("read:scope_denied", settings.lastError)
        TransportRuntime.finished(context, ExchangeSummary())
        assertEquals("read:scope_denied", settings.lastError)
        server.clearFailures()
        PhoneReadModels.forContext(context, debug = false).snapshot()
        assertNull(settings.lastError)
    }
    @Test fun wikiErrorsStaySeparateFromSnapshotSkipsAndClearOnSuccessAnd304() = runBlocking {
        pairFollowing()
        server.serveDerived(Fx.threadPages.first()) { body ->
            val root = body.jsonObject
            val items = (root["items"] as JsonArray).toMutableList()
            items[0] = JsonObject(items[0].jsonObject.toMutableMap().also { it["kind"] = JsonPrimitive("미지") })
            JsonObject(root.toMutableMap().also { it["items"] = JsonArray(items) })
        }
        server.serve(*Fx.threadPages.drop(1).toTypedArray(), "read.tags", "wiki.list", "wiki.page")
        val model = PhoneReadModels.forContext(context, debug = false)
        val page = model.snapshot().wikiPages.first { it.title == "방화벽 교체" }
        val settings = TransportRuntime.settings(context)
        server.failWith("/read/wiki/pages/${page.id}", "errors.scope_denied")
        assertNull(model.wikiBody(page))
        assertEquals("unknown_thread_kind:1", settings.lastReadError)
        assertEquals("scope_denied", settings.lastWikiError)
        assertEquals("read:unknown_thread_kind:1,scope_denied", settings.lastError)
        model.snapshot()
        assertEquals("scope_denied", settings.lastWikiError)
        server.clearFailures()
        val body = model.wikiBody(page)
        assertNotNull(body)
        assertNull(settings.lastWikiError)
        assertEquals("read:unknown_thread_kind:1", settings.lastError)
        server.failWith("/read/wiki/pages/${page.id}", "errors.scope_denied")
        assertEquals(body, model.wikiBody(page))
        assertEquals("scope_denied", settings.lastWikiError)
        server.clearFailures()
        server.reset("wiki.page"); server.serve("wiki.page_not_modified")
        assertEquals(body, model.wikiBody(page))
        assertNull(settings.lastWikiError)
        assertEquals("read:unknown_thread_kind:1", settings.lastError)
    }

    @Test fun malformedCapabilitiesAreAFormatErrorAndNotAnOfflineRun() {
        pairFollowing()
        val previous = TransportRuntime.apiFactory
        try {
            TransportRuntime.apiFactory = { pairing ->
                object : MobileApi by HttpsMobileApi(pairing) {
                    override fun capabilities(): JsonObject = throw InvalidResponseException()
                }
            }
            val summary = TransportRuntime.refresh(context)
            assertFalse(summary.unreachable)
            assertEquals("invalid_read_response", summary.errorCode)
        } finally { TransportRuntime.apiFactory = previous }
    }

}
