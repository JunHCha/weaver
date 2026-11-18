package io.github.junhcha.weaver.transport

import io.github.junhcha.weaver.courier.CourierClient
import io.github.junhcha.weaver.schema.*
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

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class HttpExchangeTest {
    @get:Rule val tmp = TemporaryFolder()
    private val server = FakeMobileServer()
    private lateinit var spool: SpoolFolder
    private lateinit var state: TransportState
    private val allHttp: (TransportFeature) -> TransportRoute = { TransportRoute.HTTP }

    private val id = "cap_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    private val body = "synthetic text".toByteArray()
    private val sha = CourierClient.digest(body.inputStream()).first
    private val manifest = Manifest(1, id, CaptureKind.TEXT, Channel.SHARE_TEXT, "$id.txt", sha, body.size.toLong(),
        "2026-11-07T00:00:00Z", null, Manifest.Hints(null))
    private fun example(name: String) = File(SharedSchema.dir, "examples/$name.json").readText()
    private val pkgId = "pkg_01ARZ3NDEKTSV4RRFFQ69G5FA0"

    @Before fun setup() {
        spool = SpoolFolder(tmp.newFolder("spool"))
        state = TransportState(File(tmp.newFolder("state"), "state.json"))
    }
    @After fun stop() = server.close()

    private fun exchange(live: FakeMobileServer = server): HttpExchange = HttpExchange(
        HttpsMobileApi(FakeMobileServer.pairing(live), connectTimeoutMs = 1000, readTimeoutMs = 1000), spool, state, allHttp, { true },
    )

    private fun spoolCapture() {
        spool.publish("outbox", manifest.payload) { it.write(body) }
        spool.publish("outbox", "$id.json") { it.write(WeaverCodec.encode(manifest).toByteArray()) }
    }

    // ---- capture: payload -> manifest -> Receipt ----

    @Test fun captureUploadsPayloadThenManifestAndKeepsTheLocalCopyUntilTheReceiptIsOnDisk() {
        spoolCapture()
        val run = exchange().push()
        assertFalse(run.retry)
        assertEquals(1, server.payloadPuts); assertEquals(1, server.manifestPuts)
        assertTrue(spool.exists("receipts", "$id.json"))
        assertFalse(spool.exists("outbox", "$id.json")); assertFalse(spool.exists("outbox", manifest.payload))
        assertEquals(sha, CourierClient.decoded<Receipt>(spool.read("receipts", "$id.json").use { it.readBytes().decodeToString() }).sha256)
        assertEquals(listOf("PUT /mobile/v1/captures/$id/payload", "PUT /mobile/v1/captures/$id/manifest"),
            server.requests.filter { it.startsWith("PUT") })
    }

    @Test fun macOffKeepsTheQueueAndTheItemIsDeliveredWhenTheServerComesBack() {
        spoolCapture()
        val port = server.port
        val pairing = FakeMobileServer.pairing(server)
        server.close()
        val down = HttpExchange(HttpsMobileApi(pairing.copy(baseUrl = "https://127.0.0.1:$port"), connectTimeoutMs = 300, readTimeoutMs = 300), spool, state, allHttp)
        val summary = down.push()
        assertTrue(summary.unreachable && summary.retry)
        assertEquals("unreachable", summary.errorCode)
        assertTrue("payload and manifest stay queued", spool.exists("outbox", "$id.json") && spool.exists("outbox", manifest.payload))
        assertEquals(1, HttpExchange.queue(spool, state).captures)
        // The Mac is back (a new server instance under the same files): the same IDs and bytes go out.
        FakeMobileServer().use { back ->
            val ok = exchange(back).push()
            assertFalse(ok.retry)
            assertTrue(back.receipts.containsKey(id))
            assertEquals(0, HttpExchange.queue(spool, state).captures)
        }
    }

    @Test fun lostManifestResponseIsRecoveredByLookingUpTheReceiptWithoutResendingTheBody() {
        spoolCapture()
        server.dropResponse.add("/manifest")
        val first = exchange().push()
        assertTrue("an unanswered request is retried", first.retry)
        assertTrue(spool.exists("outbox", "$id.json"))
        assertEquals(1, server.payloadPuts)
        val second = exchange().push()
        assertFalse(second.retry)
        assertEquals("body is not sent again", 1, server.payloadPuts)
        assertEquals("same manifest is not re-committed", 1, server.manifestPuts)
        assertTrue(spool.exists("receipts", "$id.json"))
    }

    @Test fun resendingTheSameCaptureIsIdempotentOnTheServer() {
        spoolCapture()
        exchange().push()
        val firstReceipt = server.receipts.getValue(id)
        spoolCapture()
        spool.delete("receipts", "$id.json")
        exchange().push()
        assertEquals(firstReceipt, server.receipts.getValue(id))
        assertEquals(1, server.receipts.size)
    }

    @Test fun aRejectedCaptureIsBlockedWithItsCodeAndDoesNotHoldBackTheOthers() {
        val otherId = "cap_01ARZ3NDEKTSV4RRFFQ69G5FAW"
        spoolCapture()
        val other = manifest.copy(captureId = otherId, payload = "$otherId.txt")
        spool.publish("outbox", other.payload) { it.write(body) }
        spool.publish("outbox", "$otherId.json") { it.write(WeaverCodec.encode(other).toByteArray()) }
        server.fail["/captures/$id/payload"] = 422
        val run = exchange().push()
        assertFalse("a rejected item needs the user, not a retry loop", run.retry)
        assertEquals("injected_422", run.errorCode)
        assertTrue(spool.exists("receipts", "$otherId.json"))
        assertTrue("rejected capture is kept, never dropped", spool.exists("outbox", "$id.json"))
        assertEquals(1, HttpExchange.queue(spool, state).blocked)
        server.fail.clear()
        exchange().push()
        assertTrue("still blocked until the user retries", spool.exists("outbox", "$id.json"))
        state.update { it.copy(blocked = emptyMap()) }
        exchange().push()
        assertTrue(spool.exists("receipts", "$id.json"))
    }

    // ---- 403 classification (contract item 10) ----

    private fun denied(code: String, status: Int = 403) {
        spoolCapture()
        server.fail["/receipt"] = status; server.failCodes["/receipt"] = code
    }

    @Test fun nodeMismatchMeansPairAgainAndStopsTheRun() {
        denied("node_mismatch")
        val run = exchange().push()
        assertTrue(run.needsRepair); assertFalse(run.retry)
        assertTrue(spool.exists("outbox", "$id.json"))
    }

    @Test fun scopeDeniedBlocksOnlyThatFeatureAndKeepsTheOthersRunning() {
        publishReview(); exchange().pull()
        spool.publish("decisions", "$pkgId.r1.json") { it.write(WeaverCodec.encode(verdict()).toByteArray()) }
        denied("scope_denied")
        val run = exchange().push()
        assertFalse("not a re-pair", run.needsRepair); assertFalse(run.retry)
        assertEquals("scope_denied", state.load().blockedFeatures["capture"])
        assertTrue("capture stays queued, nothing is dropped", spool.exists("outbox", "$id.json"))
        assertEquals("the verdict feature was not blocked", 1, server.verdictPuts)
        server.fail.clear()
        exchange().push()
        assertEquals("the blocked feature does not retry by itself", 0, server.payloadPuts)
        state.update { it.copy(blockedFeatures = emptyMap()) }
        exchange().push()
        assertTrue(spool.exists("receipts", "$id.json"))
    }

    @Test fun notExecutorBlocksTheActionFeatureOnly() {
        server.actions[actionId] = example("Action")
        server.fail["/actions"] = 403; server.failCodes["/actions"] = "not_executor"
        val run = exchange().pull()
        assertFalse(run.needsRepair)
        assertEquals("not_executor", state.load().blockedFeatures["action"])
    }

    @Test fun whoisUnavailableIs503AndRetried() {
        denied("whois_unavailable", status = 503)
        val run = exchange().push()
        assertTrue(run.retry); assertFalse(run.needsRepair)
        assertTrue(state.load().blockedFeatures.isEmpty() && state.load().blocked.isEmpty())
    }

    @Test fun otherConfirmedForbiddenStopsTheRunWithoutAskingToPairAgain() {
        denied("peer_unverified")
        val run = exchange().push()
        assertFalse(run.needsRepair); assertFalse(run.retry)
        assertEquals("peer_unverified", run.errorCode)
    }

    @Test fun alertsAndPipelineAreOnlyRequestedWhenTheServerOffersThem() {
        server.alerts["alt_01ARZ3NDEKTSV4RRFFQ69G5FA0"] = example("Alert")
        server.pipeline = listOf(example("PipelineItem"))
        val off = HttpExchange(HttpsMobileApi(FakeMobileServer.pairing(server)), spool, state, allHttp)
        off.pull(); off.push()
        assertTrue(server.requests.none { it.contains("/alerts") || it.contains("/pipeline") })
        assertTrue(spool.list("proposals/pipeline").isEmpty())
    }

    @Test fun aLostVerdictResponseIsNotMistakenForStaleWhenTheNewRevisionAlreadyReplacedThePackage() {
        publishReview(1); exchange().pull()
        spool.publish("decisions", "$pkgId.r1.json") { it.write(WeaverCodec.encode(verdict(1)).toByteArray()) }
        server.dropResponse.add("/verdict")
        assertTrue(exchange().push().retry) // the server accepted it, the answer was lost
        // The package moves on and the old one leaves the list; the answer's ETag must survive that.
        publishReview(2); server.reviews.remove(pkgId to 1L)
        server.reviews[pkgId to 1L] = example("ReviewPackage") to "\"e1\""
        exchange().pull()
        assertTrue("etag kept for the pending answer", state.load().etags.containsKey("$pkgId.r1"))
        exchange().push()
        assertNull("not stale: the server returned the operation it already created", state.load().verdictOutcomes["$pkgId.r1.json"])
        assertEquals(1, server.operations.size)
        assertEquals(1, state.load().verdictOps.size)
    }

    @Test fun aRejectedTokenStopsTheRunAndAsksForPairingAgain() {
        spoolCapture()
        val bad = HttpExchange(HttpsMobileApi(FakeMobileServer.pairing(server, token = "x".repeat(43))), spool, state, allHttp)
        val run = bad.push()
        assertTrue(run.needsRepair); assertFalse(run.retry)
        assertTrue(spool.exists("outbox", "$id.json"))
    }

    // ---- reviews, verdicts, operations ----

    private fun publishReview(rev: Long = 1, etag: String = "\"e$rev\"") {
        val text = example("ReviewPackage").replace("\"revision\": 1", "\"revision\": $rev")
        server.reviews[pkgId to rev] = text to etag
    }

    private fun verdict(rev: Long = 1) = Verdict(2, pkgId, rev, Verdict.Action.APPROVE, listOf("네"), null, null)

    @Test fun pullStoresReviewPackagesAndTheirEtag() {
        publishReview()
        exchange().pull()
        assertTrue(spool.exists("proposals", "$pkgId.r1.json"))
        assertEquals("\"e1\"", state.load().etags["$pkgId.r1"])
    }

    @Test fun verdictIsAcceptedWithIfMatchThenKeptUntilTheOperationIsApplied() {
        publishReview(); exchange().pull()
        spool.publish("decisions", "$pkgId.r1.json") { it.write(WeaverCodec.encode(verdict()).toByteArray()) }
        val first = exchange().push()
        assertFalse(first.retry)
        assertEquals(1, server.verdictPuts)
        assertTrue("202 is acceptance, not application: the verdict stays queued", spool.exists("decisions", "$pkgId.r1.json"))
        assertEquals(1, HttpExchange.queue(spool, state).verdicts)
        server.applyAll()
        exchange().push()
        assertFalse(spool.exists("decisions", "$pkgId.r1.json"))
        assertEquals("APPLIED", state.load().verdictOutcomes["$pkgId.r1.json"])
        assertEquals("no second PUT for the same operation", 1, server.verdictPuts)
    }

    @Test fun lostAcceptanceResponseResendsTheSameVerdictAndGetsTheSameOperation() {
        publishReview(); exchange().pull()
        spool.publish("decisions", "$pkgId.r1.json") { it.write(WeaverCodec.encode(verdict()).toByteArray()) }
        server.dropResponse.add("/verdict")
        assertTrue(exchange().push().retry)
        assertEquals(1, server.verdictPuts)
        assertTrue(state.load().verdictOps.isEmpty())
        exchange().push()
        assertEquals(2, server.verdictPuts)
        assertEquals("same logical key and content map to one operation", 1, server.operations.size)
        assertEquals(1, server.verdicts.size)
    }

    @Test fun aNewerRevisionAnswersStaleAndTheOldVerdictIsNeverAppliedToIt() {
        publishReview(1); exchange().pull()
        spool.publish("decisions", "$pkgId.r1.json") { it.write(WeaverCodec.encode(verdict(1)).toByteArray()) }
        publishReview(2)
        val run = exchange().push()
        assertFalse("stale is final, not a retry", run.retry)
        assertEquals("STALE", state.load().verdictOutcomes["$pkgId.r1.json"])
        assertTrue("no operation was created for the old revision", server.operations.isEmpty())
        assertFalse(spool.exists("decisions", "$pkgId.r1.json"))
    }

    @Test fun aDifferentVerdictForTheSameRevisionIsAConflict() {
        publishReview(); exchange().pull()
        server.verdicts[pkgId to 1L] = "{\"other\":true}" to "op_x"
        spool.publish("decisions", "$pkgId.r1.json") { it.write(WeaverCodec.encode(verdict()).toByteArray()) }
        exchange().push()
        assertEquals("CONFLICT", state.load().verdictOutcomes["$pkgId.r1.json"])
    }

    @Test fun withdrawnPackagesAreRemovedOnlyAfterACompleteListing() {
        publishReview(); exchange().pull()
        server.fail["/reviews"] = 503
        exchange().pull()
        assertTrue("a failed listing deletes nothing", spool.exists("proposals", "$pkgId.r1.json"))
        server.fail.clear(); server.reviews.clear()
        exchange().pull()
        assertFalse(spool.exists("proposals", "$pkgId.r1.json"))
    }

    // ---- alerts ----

    @Test fun dismissedAlertIsAckedOnceAndNeverComesBack() {
        val alert = example("Alert")
        server.alerts["alt_01ARZ3NDEKTSV4RRFFQ69G5FA0"] = alert
        exchange().pull()
        assertTrue(spool.exists("proposals", "alert_alt_01ARZ3NDEKTSV4RRFFQ69G5FA0.json"))
        exchange().push()
        assertTrue("still shown: nothing acked yet", server.acked.isEmpty())
        spool.delete("proposals", "alert_alt_01ARZ3NDEKTSV4RRFFQ69G5FA0.json") // the user dismissed it
        exchange().push()
        assertEquals(listOf("alt_01ARZ3NDEKTSV4RRFFQ69G5FA0"), server.acked)
        exchange().pull()
        assertFalse(spool.exists("proposals", "alert_alt_01ARZ3NDEKTSV4RRFFQ69G5FA0.json"))
    }

    // ---- actions ----

    private val actionId = "act_0123456789abcdef0123456789abcdef"

    @Test fun actionResultIsKeptUntilTheServerAppliedItThenTheActionIsRetired() {
        server.actions[actionId] = example("Action")
        exchange().pull()
        assertTrue(spool.exists("actions", "$actionId.json"))
        spool.publish("action-results", "$actionId.json") { it.write(example("ActionResult").toByteArray()) }
        exchange().push()
        assertEquals(1, server.resultPuts)
        assertTrue(spool.exists("action-results", "$actionId.json"))
        server.applyAll()
        exchange().push()
        assertFalse(spool.exists("action-results", "$actionId.json"))
        assertFalse(spool.exists("actions", "$actionId.json"))
        assertTrue(actionId in state.load().actionsDone)
        exchange().pull()
        assertFalse("an applied action is never downloaded again", spool.exists("actions", "$actionId.json"))
    }

    @Test fun lostActionResultResponseIsResentWithoutADuplicate() {
        server.actions[actionId] = example("Action")
        exchange().pull()
        spool.publish("action-results", "$actionId.json") { it.write(example("ActionResult").toByteArray()) }
        server.dropResponse.add("/result")
        assertTrue(exchange().push().retry)
        exchange().push()
        assertEquals(2, server.resultPuts)
        assertEquals(1, server.operations.size)
    }

    @Test fun conflictingActionResultIsBlockedNotRetried() {
        server.actions[actionId] = example("Action")
        exchange().pull()
        server.results[actionId] = "{\"different\":1}" to "op_z"
        spool.publish("action-results", "$actionId.json") { it.write(example("ActionResult").toByteArray()) }
        val run = exchange().push()
        assertFalse(run.retry)
        assertEquals(1, HttpExchange.queue(spool, state).blocked)
        assertTrue(spool.exists("action-results", "$actionId.json"))
    }

    // ---- pipeline ----

    @Test fun pipelineItemsAreMirroredFromTheCompleteListing() {
        server.pipeline = listOf(example("PipelineItem"))
        exchange().pull()
        assertTrue(spool.exists("proposals/pipeline", "src_01ARZ3NDEKTSV4RRFFQ69G5FAV.json"))
        server.pipeline = emptyList()
        exchange().pull()
        assertFalse(spool.exists("proposals/pipeline", "src_01ARZ3NDEKTSV4RRFFQ69G5FAV.json"))
    }

    @Test fun onlyTheFeaturesRoutedOverHttpTouchTheServer() {
        spoolCapture()
        val onlyVerdict = HttpExchange(HttpsMobileApi(FakeMobileServer.pairing(server)), spool, state,
            { if (it == TransportFeature.VERDICT) TransportRoute.HTTP else TransportRoute.FILES })
        onlyVerdict.pull(); onlyVerdict.push()
        assertTrue(server.requests.none { it.contains("captures") || it.contains("actions") })
        assertTrue(spool.exists("outbox", "$id.json"))
    }
}
