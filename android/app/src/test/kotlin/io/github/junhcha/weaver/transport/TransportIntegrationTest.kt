package io.github.junhcha.weaver.transport

import android.content.Context
import android.net.Uri
import androidx.test.core.app.ApplicationProvider
import androidx.work.ListenableWorker
import androidx.work.testing.WorkManagerTestInitHelper
import io.github.junhcha.weaver.courier.CourierDatabase
import io.github.junhcha.weaver.courier.CourierRuntime
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
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** The whole phone path against the local test server: pairing, server-driven routes, queue, retry, migration, reviews. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class TransportIntegrationTest {
    @get:Rule val tmp = TemporaryFolder()
    private lateinit var context: Context
    private lateinit var server: FakeMobileServer
    private val extra = mutableListOf<FakeMobileServer>()
    private val pkgId = "pkg_01ARZ3NDEKTSV4RRFFQ69G5FA0"
    private val actionId = "act_0123456789abcdef0123456789abcdef"
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
    @After fun stop() { server.close(); extra.forEach { it.close() } }

    private fun example(name: String) = File(SharedSchema.dir, "examples/$name.json").readText()

    /** Pairs and lets the server's capabilities decide the routes, exactly as the worker does. */
    private fun pairFollowing(transports: Map<String, String> = allHttp, features: Map<String, Boolean> = emptyMap()) {
        server.transports = transports; server.features = features
        TransportRuntime.pair(context, FakeMobileServer.code(server))
        TransportRuntime.refresh(context)
    }

    private fun capture(text: String = "synthetic"): Manifest {
        val file = tmp.newFile("source-${System.nanoTime()}.txt").apply { writeText(text) }
        return CourierRuntime.prepare(context, Uri.fromFile(file), CaptureKind.TEXT, Channel.SHARE_TEXT, "txt")
    }

    private fun run(): ListenableWorker.Result = CourierWorker.run(context, network = true)

    // ---- pairing ----

    @Test fun pairingTrustsOnlyTheServerWhoseCertificateChainsToTheCodesCa() {
        val foreign = FakeMobileServer(pki = TestPki.create()).also { extra += it }
        assertThrows(PinMismatchException::class.java) { TransportRuntime.pair(context, FakeMobileServer.code(foreign, pki = TestPki.default)) }
        assertFalse(TransportRuntime.settings(context).paired)
        assertThrows(ApiException::class.java) { TransportRuntime.pair(context, FakeMobileServer.code(server, token = "x".repeat(43))) }
        assertFalse(TransportRuntime.settings(context).paired)
        assertThrows("the code names another server than the one that answered", PairingException::class.java) {
            TransportRuntime.pair(context, FakeMobileServer.code(server, serverId = "srv-other"))
        }
        assertFalse(TransportRuntime.settings(context).paired)
        val result = TransportRuntime.pair(context, FakeMobileServer.code(server))
        assertEquals("srv-test", result.serverId)
        assertTrue(TransportRuntime.settings(context).paired)
    }

    @Test fun pairingStartsOnSyncthingAndTheServerCapabilitiesSwitchTheRoutes() {
        server.transports = allHttp
        TransportRuntime.pair(context, FakeMobileServer.code(server))
        // pair() already applied them: the server is the single source of truth.
        TransportFeature.entries.forEach { assertEquals(TransportRoute.HTTP, TransportRuntime.settings(context).route(it)) }
        server.transports = mapOf("capture" to "files", "review" to "http", "action" to "files")
        TransportRuntime.refresh(context)
        assertEquals(TransportRoute.FILES, TransportRuntime.settings(context).route(TransportFeature.CAPTURE))
        assertEquals(TransportRoute.HTTP, TransportRuntime.settings(context).route(TransportFeature.VERDICT))
    }

    @Test fun revokedNodeShowsRePairAndRetryBlockedDoesNotHideIt() {
        pairFollowing(); capture()
        server.fail["/receipt"] = 403; server.failCodes["/receipt"] = "node_mismatch"
        assertEquals(ListenableWorker.Result.success(), run())
        val settings = TransportRuntime.settings(context)
        assertTrue(settings.needsRepair)
        assertEquals(1, TransportRuntime.queue(context).captures)
        assertNull("no exchange while the node is rejected", TransportRuntime.exchange(context))
        TransportRuntime.retryBlocked(context)
        assertTrue("retry must not clear a real re-pair need", settings.needsRepair)
        server.fail.clear()
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

    @Test fun capturesQueueWhileTheMacIsOffAndArriveOnceWhenItReturns() {
        pairFollowing()
        server.offline = true
        val first = capture("one"); val second = capture("two")
        assertEquals(ListenableWorker.Result.retry(), run())
        val settings = TransportRuntime.settings(context)
        assertEquals("unreachable", settings.lastError)
        assertEquals(2, TransportRuntime.queue(context).captures)
        assertTrue(server.receipts.isEmpty())
        server.offline = false
        assertEquals(ListenableWorker.Result.success(), run())
        assertEquals(setOf(first.captureId, second.captureId), server.receipts.keys)
        assertEquals(0, TransportRuntime.queue(context).captures)
        assertNull(settings.lastError)
        assertTrue(settings.lastSyncAt > 0)
        CourierDatabase(context).use { db -> assertTrue(db.captures().all { it.state == "RECEIVED" }) }
        run()
        assertEquals("a finished capture is not uploaded again", 2, server.payloadPuts)
    }

    @Test fun lostResponsesAcrossRunsNeverDuplicateOrLoseACapture() {
        pairFollowing()
        val manifest = capture()
        server.dropResponse.add("/manifest")
        assertEquals(ListenableWorker.Result.retry(), run())
        assertEquals(ListenableWorker.Result.success(), run())
        assertEquals(1, server.receipts.size)
        assertEquals(manifest.captureId, server.receipts.keys.single())
        assertEquals(1, server.manifestPuts)
    }

    // ---- route changes and unpairing move every queued item with its original ID ----

    private fun queueEverythingOffline(): Triple<Manifest, String, String> {
        pairFollowing()
        server.offline = true
        val manifest = capture("keep my bytes")
        run() // the capture reaches the spool, the Mac is off
        val spool = TransportRuntime.spool(context)
        val verdictName = "$pkgId.r1.json"
        spool.publish("decisions", verdictName) {
            it.write(WeaverCodec.encode(Verdict(2, pkgId, 1, Verdict.Action.APPROVE, listOf("네"), null, null)).toByteArray())
        }
        spool.publish("action-results", "$actionId.json") { it.write(example("ActionResult").toByteArray()) }
        val queue = TransportRuntime.queue(context)
        assertEquals(1, queue.captures); assertEquals(1, queue.verdicts); assertEquals(1, queue.results)
        return Triple(manifest, verdictName, "$actionId.json")
    }

    @Test fun whenTheServerSwitchesFeaturesBackToSyncthingEveryUnsentItemMovesWithItsOriginalId() {
        val (manifest, verdictName, resultName) = queueEverythingOffline()
        server.transports = mapOf("capture" to "files", "review" to "files", "action" to "files")
        server.offline = false
        TransportRuntime.refresh(context)
        val files = MemoryFolder()
        assertEquals(4, TransportRuntime.migrateOrphans(context, files))
        assertTrue(files.files.containsKey("outbox/${manifest.payload}"))
        assertEquals("keep my bytes", files.files.getValue("outbox/${manifest.payload}").decodeToString())
        assertTrue(files.files.containsKey("outbox/${manifest.captureId}.json"))
        assertTrue(files.files.containsKey("decisions/$verdictName"))
        assertTrue(files.files.containsKey("action-results/$resultName"))
        assertTrue("payload goes before its manifest", files.published.indexOf(manifest.payload) < files.published.indexOf("${manifest.captureId}.json"))
        assertEquals(QueueCounts(0, 0, 0, 0), TransportRuntime.queue(context))
        assertEquals(0, TransportRuntime.migrateOrphans(context, files))
    }

    @Test fun unpairingMovesQueuedItemsToSyncthingAndLeavesNothingOrphaned() {
        val (manifest, _, _) = queueEverythingOffline()
        TransportRuntime.unpair(context)
        // Syncthing is not selected in this test, so nothing may be dropped: the items wait in the spool...
        assertEquals(3, TransportRuntime.queue(context).total)
        val files = MemoryFolder()
        TransportRuntime.migrateOrphans(context, files)
        assertEquals(0, TransportRuntime.queue(context).total)
        assertTrue(files.files.containsKey("outbox/${manifest.captureId}.json"))
        assertEquals(TransportRoute.FILES, TransportRuntime.settings(context).route(TransportFeature.CAPTURE))
    }

    @Test fun nothingMovesWhileTheSyncthingFolderIsUnavailable() {
        queueEverythingOffline()
        server.transports = mapOf("capture" to "files", "review" to "files", "action" to "files")
        server.offline = false
        TransportRuntime.refresh(context)
        assertEquals(0, TransportRuntime.migrateOrphans(context, io.github.junhcha.weaver.transport.MissingFolder))
        assertEquals(3, TransportRuntime.queue(context).total)
    }

    @Test fun aSwitchToHttpResendsAnUnconfirmedCaptureOverHttpWithTheSameIdAndBytes() {
        TransportRuntime.pair(context, FakeMobileServer.code(server))
        val manifest = capture("same bytes")
        val files = MemoryFolder()
        val settings = TransportRuntime.settings(context)
        val routed = RoutedFolder(files, TransportRuntime.spool(context), settings::route)
        CourierDatabase(context).use { db -> CourierWorker.synchronize(context, routed, db) }
        val filePayload = files.files.getValue("outbox/${manifest.payload}")
        server.transports = allHttp
        TransportRuntime.refresh(context)
        CourierDatabase(context).use { db -> CourierWorker.synchronize(context, routed, db, TransportRuntime.exchange(context)) }
        assertEquals(setOf(manifest.captureId), server.receipts.keys)
        assertEquals("same bytes", server.payloads.getValue(manifest.captureId).decodeToString())
        assertEquals("same bytes", filePayload.decodeToString())
    }

    @Test fun mixedRoutesKeepCapturesOnSyncthingAndSendOnlyTheChosenFeaturesOverHttp() {
        pairFollowing(mapOf("capture" to "files", "review" to "http", "action" to "files"))
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
        capture("belongs to server A")
        run()
        assertEquals(1, TransportRuntime.queue(context).captures)
        val second = FakeMobileServer(serverId = "srv-second").also { extra += it }
        second.transports = allHttp
        val result = TransportRuntime.pair(context, FakeMobileServer.code(second))
        assertEquals("srv-second", result.serverId)
        assertEquals(1, result.otherServerQueued)
        assertEquals("the new server starts with an empty queue", 0, TransportRuntime.queue(context).total)
        assertEquals(1, TransportRuntime.otherServerQueue(context))
        TransportRuntime.refresh(context)
        run()
        // The phone's own capture log is not tied to a server, so the unconfirmed capture is offered to the new server
        // through the new spool; server A's copy in A's spool is untouched (same ID: re-pairing A later stays idempotent).
        assertEquals("http:srv-second", TransportRuntime.routed(context, MissingFolder).identity)
        assertEquals(1, TransportRuntime.otherServerQueue(context))
    }

    // ---- review round trip ----

    @Test fun reviewArrivesInTheInboxIsAnsweredOverHttpAndEndsAsApplied() {
        pairFollowing()
        server.reviews[pkgId to 1L] = example("ReviewPackage") to "\"e1\""
        assertEquals(ListenableWorker.Result.success(), run())
        assertEquals(listOf(pkgId), InboxRuntime.reviews(context).map { it.packageId })
        val pkg = ReviewRuntime.open(context, pkgId)
        val verdict = Verdict(2, pkgId, 1, Verdict.Action.APPROVE, pkg.questions.map { "네" }, null, null)
        ReviewRuntime.submit(context, pkg, verdict)
        assertEquals(ListenableWorker.Result.success(), run())
        assertEquals(1, server.verdicts.size)
        assertEquals("accepted, not yet applied", ReviewStatus.SENT, ReviewRuntime.records(context).single().status)
        server.applyAll()
        run()
        assertEquals(ReviewStatus.APPLIED, ReviewRuntime.records(context).single().status)
        assertEquals(0, TransportRuntime.queue(context).verdicts)
    }

    @Test fun anAnswerToASupersededRevisionIsMarkedStaleAndTheDraftTextIsKept() {
        pairFollowing()
        server.reviews[pkgId to 1L] = example("ReviewPackage") to "\"e1\""
        run()
        val pkg = ReviewRuntime.open(context, pkgId)
        val verdict = Verdict(2, pkgId, 1, Verdict.Action.APPROVE, pkg.questions.map { "내 답" }, null, null)
        // A new revision lands on the server while the phone still shows revision 1; submitting kicks the workers.
        server.reviews[pkgId to 2L] = example("ReviewPackage").replace("\"revision\": 1", "\"revision\": 2") to "\"e2\""
        ReviewRuntime.submit(context, pkg, verdict)
        run()
        val record = ReviewRuntime.records(context).single()
        assertEquals(ReviewStatus.STALE, record.status)
        assertEquals("the person's answers are preserved", pkg.questions.map { "내 답" }, record.verdict.answers)
        assertTrue(server.operations.isEmpty())
    }

    @Test fun alertsStayOnTheSyncthingFolderUntilTheServerOffersThemOverHttp() {
        pairFollowing(features = emptyMap())
        server.alerts["alt_01ARZ3NDEKTSV4RRFFQ69G5FA0"] = example("Alert")
        val files = MemoryFolder()
        files.put("proposals", "alert_alt_01ARZ3NDEKTSV4RRFFQ69G5FA9.json", example("Alert").replace("FA0", "FA9"))
        val routed = RoutedFolder(files, TransportRuntime.spool(context), TransportRuntime.settings(context)::route, TransportRuntime.settings(context)::feature)
        assertEquals("the alert file still comes from Syncthing while review runs over HTTP",
            listOf("alert_alt_01ARZ3NDEKTSV4RRFFQ69G5FA9.json"), routed.list("proposals").map { it.name })
        assertTrue(server.requests.none { it.contains("/alerts") })
        server.features = mapOf("alerts" to true)
        TransportRuntime.refresh(context)
        run()
        assertEquals(1, InboxRuntime.alerts(context).size)
        assertTrue(server.requests.any { it.contains("/alerts") })
        assertTrue(InboxRuntime.dismissAlert(context, "alt_01ARZ3NDEKTSV4RRFFQ69G5FA0"))
        run()
        assertEquals(listOf("alt_01ARZ3NDEKTSV4RRFFQ69G5FA0"), server.acked)
    }

    // ---- read model problems reach the connection card ----

    @Test fun anUnknownThreadKindIsCountedIntoLastErrorNotSilentlyDropped() = runBlocking {
        pairFollowing()
        server.threads = listOf(
            """{"thread_id":"a","seq":1,"kind":"요청","content":"x","occurred_at":"2026-11-10T01:00:00Z","recorded_at":"2026-11-10T01:00:00Z","tags":[],"source":"통화"}""",
            """{"thread_id":"b","seq":2,"kind":"미지","content":"x","occurred_at":"2026-11-10T01:00:00Z","recorded_at":"2026-11-10T01:00:00Z","tags":[],"source":"통화"}""",
        )
        server.tags = emptyList(); server.wiki = emptyList()
        val snapshot = PhoneReadModels.forContext(context, debug = false).snapshot()
        assertEquals(1, snapshot.threads.size)
        assertEquals("unknown_thread_kind:1", TransportRuntime.settings(context).lastError)
        server.threads = server.threads.take(1)
        PhoneReadModels.forContext(context, debug = false).snapshot()
        assertNull("a clean read clears the note", TransportRuntime.settings(context).lastError)
    }
}
