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
import io.github.junhcha.weaver.review.ReviewRuntime
import io.github.junhcha.weaver.review.ReviewStatus
import io.github.junhcha.weaver.schema.*
import io.github.junhcha.weaver.storage.AppStorage
import java.io.File
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** The whole phone path against the local test server: pairing, queue, retry, routes, review round trip. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class TransportIntegrationTest {
    @get:Rule val tmp = TemporaryFolder()
    private lateinit var context: Context
    private lateinit var server: FakeMobileServer
    private val pkgId = "pkg_01ARZ3NDEKTSV4RRFFQ69G5FA0"

    @Before fun setup() {
        context = ApplicationProvider.getApplicationContext()
        listOf("transport", "courier").forEach { context.getSharedPreferences(it, Context.MODE_PRIVATE).edit().clear().commit() }
        File(context.filesDir, "courier.sqlite3").delete()
        AppStorage.exchangeDir(context).deleteRecursively()
        TransportRuntime.secretBox = { ReverseBox() }
        WorkManagerTestInitHelper.initializeTestWorkManager(context)
        server = FakeMobileServer()
    }
    @After fun stop() = server.close()

    private fun code(s: FakeMobileServer = server, fp: String = s.fingerprint, token: String = s.token) =
        """{"v":1,"url":"${s.url}","fp":"$fp","token":"$token"}"""

    private fun pairAllHttp() {
        TransportRuntime.pair(context, code())
        TransportFeature.entries.forEach { TransportRuntime.setRoute(context, it, TransportRoute.HTTP) }
    }

    private fun example(name: String) = File(SharedSchema.dir, "examples/$name.json").readText()

    private fun capture(text: String = "synthetic"): Manifest {
        val file = tmp.newFile("source-${System.nanoTime()}.txt").apply { writeText(text) }
        return CourierRuntime.prepare(context, Uri.fromFile(file), CaptureKind.TEXT, Channel.SHARE_TEXT, "txt")
    }

    private fun run(): ListenableWorker.Result = CourierWorker.run(context, network = true)

    // ---- pairing ----

    @Test fun pairingTrustsOnlyTheServerWhoseCertificateMatchesTheCode() {
        assertThrows(PinMismatchException::class.java) { TransportRuntime.pair(context, code(fp = "00".repeat(32))) }
        assertFalse(TransportRuntime.settings(context).paired)
        assertThrows(ApiException::class.java) { TransportRuntime.pair(context, code(token = "x".repeat(43))) }
        assertFalse(TransportRuntime.settings(context).paired)
        assertEquals("srv-test", TransportRuntime.pair(context, code()))
        val settings = TransportRuntime.settings(context)
        assertTrue(settings.paired)
        assertEquals("srv-test", settings.serverId)
    }

    @Test fun revokedTokenShowsRePairAndStopsTheQueueUntilPairedAgain() {
        pairAllHttp(); capture()
        server.fail["/receipt"] = 403
        assertEquals(ListenableWorker.Result.success(), run())
        val settings = TransportRuntime.settings(context)
        assertTrue(settings.needsRepair)
        assertEquals(1, TransportRuntime.queue(context).captures)
        assertNull("no exchange while the token is rejected", TransportRuntime.exchange(context))
        TransportRuntime.pair(context, code())
        assertFalse(settings.needsRepair)
    }

    // ---- queue: offline, retry, idempotence ----

    @Test fun capturesQueueWhileTheMacIsOffAndArriveOnceWhenItReturns() {
        pairAllHttp()
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
        pairAllHttp()
        val manifest = capture()
        server.dropResponse.add("/manifest")
        assertEquals(ListenableWorker.Result.retry(), run())
        assertEquals(ListenableWorker.Result.success(), run())
        assertEquals(1, server.receipts.size)
        assertEquals(manifest.captureId, server.receipts.keys.single())
        assertEquals(1, server.manifestPuts)
    }

    @Test fun mixedRoutesKeepCapturesOnSyncthingAndSendOnlyTheChosenFeaturesOverHttp() {
        TransportRuntime.pair(context, code())
        TransportRuntime.setRoute(context, TransportFeature.VERDICT, TransportRoute.HTTP)
        val manifest = capture()
        val files = MemoryFolder()
        val routed = RoutedFolder(files, TransportRuntime.spool(context), TransportRuntime.settings(context)::route)
        CourierDatabase(context).use { db -> CourierWorker.synchronize(context, routed, db) }
        assertTrue("capture stays on the Syncthing folder", files.files.containsKey("outbox/${manifest.captureId}.json"))
        assertTrue(TransportRuntime.spool(context).list("outbox").isEmpty())
        assertTrue(server.requests.none { it.contains("captures") })
    }

    @Test fun switchingTheRouteKeepsTheCaptureIdAndPublishesTheSameBytes() {
        TransportRuntime.pair(context, code())
        val manifest = capture("same bytes")
        val files = MemoryFolder()
        val settings = TransportRuntime.settings(context)
        val routed = RoutedFolder(files, TransportRuntime.spool(context), settings::route)
        // First over Syncthing...
        CourierDatabase(context).use { db -> CourierWorker.synchronize(context, routed, db) }
        val filePayload = files.files.getValue("outbox/${manifest.payload}")
        // ...then the user switches the feature to HTTP; a late Receipt/new capture uses the same ID rules.
        TransportRuntime.setRoute(context, TransportFeature.CAPTURE, TransportRoute.HTTP)
        val second = capture("second")
        CourierDatabase(context).use { db -> CourierWorker.synchronize(context, routed, db, TransportRuntime.exchange(context)) }
        // The first capture had no Receipt yet, so it is re-sent over HTTP under the same ID with the same bytes.
        assertEquals(setOf(manifest.captureId, second.captureId), server.receipts.keys)
        assertEquals("same bytes", server.payloads.getValue(manifest.captureId).decodeToString())
        assertEquals("same bytes", filePayload.decodeToString())
    }

    // ---- review round trip ----

    @Test fun reviewArrivesInTheInboxIsAnsweredOverHttpAndEndsAsApplied() {
        pairAllHttp()
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
        pairAllHttp()
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

    @Test fun newAlertsAreDeliveredAndAckedAfterTheUserDismissesThem() {
        pairAllHttp()
        server.alerts["alt_01ARZ3NDEKTSV4RRFFQ69G5FA0"] = example("Alert")
        run()
        assertEquals(1, InboxRuntime.alerts(context).size)
        assertTrue(InboxRuntime.dismissAlert(context, "alt_01ARZ3NDEKTSV4RRFFQ69G5FA0"))
        run()
        assertEquals(listOf("alt_01ARZ3NDEKTSV4RRFFQ69G5FA0"), server.acked)
    }

    @Test fun unpairingKeepsQueuedItemsAndFallsBackToSyncthing() {
        pairAllHttp()
        server.offline = true
        capture()
        run()
        TransportRuntime.unpair(context)
        assertEquals(TransportRoute.FILES, TransportRuntime.settings(context).route(TransportFeature.CAPTURE))
        assertEquals("nothing queued is thrown away", 1, TransportRuntime.queue(context).captures)
    }
}
