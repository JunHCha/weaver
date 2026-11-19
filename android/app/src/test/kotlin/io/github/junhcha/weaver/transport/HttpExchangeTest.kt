package io.github.junhcha.weaver.transport

import io.github.junhcha.weaver.courier.CourierClient
import io.github.junhcha.weaver.schema.*
import java.io.File
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

/** The exchange against a server that answers only with the shared contract fixtures. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class HttpExchangeTest {
    @get:Rule val tmp = TemporaryFolder()
    private val server = FakeMobileServer()
    private lateinit var spool: SpoolFolder
    private lateinit var state: TransportState
    private val allHttp: (TransportFeature) -> TransportRoute = { TransportRoute.HTTP }
    private val id = Fx.captureId
    private val vName = "${Fx.pkg}.r1.json"

    @Before fun setup() {
        spool = SpoolFolder(tmp.newFolder("spool"))
        state = TransportState(File(tmp.newFolder("state"), "state.json"))
    }
    @After fun stop() { server.close(); assertEquals("the phone sent what the fixtures expect", emptyList<String>(), server.mismatches.toList()) }

    private fun exchange(features: (ServerFeature) -> Boolean = { false }) = HttpExchange(
        HttpsMobileApi(FakeMobileServer.pairing(server), connectTimeoutMs = 1000, readTimeoutMs = 1000), spool, state, allHttp, features,
    )

    private fun spoolCapture(manifest: Manifest = Fx.manifest) {
        spool.publish("outbox", manifest.payload) { it.write(Fx.payloadText.toByteArray()) }
        spool.publish("outbox", "${manifest.captureId}.json") { it.write(WeaverCodec.encode(manifest).toByteArray()) }
    }
    private fun spoolVerdict(v: Verdict, etag: String? = null) {
        spool.publish("decisions", "${v.packageId}.r${v.revision}.json") { it.write(WeaverCodec.encode(v).toByteArray()) }
        if (etag != null) state.update { it.copy(etags = it.etags + ("${v.packageId}.r${v.revision}" to etag)) }
    }
    private val uploadFlow = arrayOf("captures.receipt_before_upload", "captures.payload_staged", "captures.manifest_stored")

    // ---- capture: payload -> manifest -> Receipt ----

    @Test fun captureUploadsPayloadThenManifestAndKeepsTheLocalCopyUntilTheReceiptIsOnDisk() {
        server.serve(*uploadFlow); spoolCapture()
        assertFalse(exchange().push().retry)
        assertEquals(listOf("GET /mobile/v1/captures/$id/receipt", "PUT /mobile/v1/captures/$id/payload", "PUT /mobile/v1/captures/$id/manifest"), server.requests)
        assertTrue(spool.exists("receipts", "$id.json"))
        assertFalse(spool.exists("outbox", "$id.json")); assertFalse(spool.exists("outbox", Fx.manifest.payload))
        assertEquals(Fx.manifest.sha256, CourierClient.decoded<Receipt>(spool.read("receipts", "$id.json").use { it.readBytes().decodeToString() }).sha256)
    }

    @Test fun macOffKeepsTheQueueAndTheItemIsDeliveredWhenTheServerComesBack() {
        server.serve(*uploadFlow); spoolCapture()
        server.offline = true
        val summary = exchange().push()
        assertTrue(summary.unreachable && summary.retry)
        assertEquals("unreachable", summary.errorCode)
        assertTrue("payload and manifest stay queued", spool.exists("outbox", "$id.json") && spool.exists("outbox", Fx.manifest.payload))
        assertEquals(1, HttpExchange.queue(spool, state).captures)
        server.offline = false
        assertFalse(exchange().push().retry)
        assertTrue(spool.exists("receipts", "$id.json"))
        assertEquals(0, HttpExchange.queue(spool, state).captures)
    }

    @Test fun lostManifestResponseIsRecoveredByLookingUpTheReceiptWithoutResendingTheBody() {
        server.serve("captures.receipt_before_upload", "captures.receipt", "captures.payload_staged", "captures.manifest_stored"); spoolCapture()
        server.dropResponse.add("/manifest")
        assertTrue("an unanswered request is retried", exchange().push().retry)
        assertTrue(spool.exists("outbox", "$id.json"))
        assertFalse(exchange().push().retry)
        assertEquals("body and manifest were sent once", 1, server.count("PUT ") / 2)
        assertEquals(listOf("GET", "PUT", "PUT", "GET").size, server.requests.size)
        assertTrue(spool.exists("receipts", "$id.json"))
    }

    @Test fun aServerThatAlreadyHoldsTheCaptureAnswersWithItsReceiptAndNothingIsResent() {
        server.serve("captures.receipt"); spoolCapture()
        assertFalse(exchange().push().retry)
        assertEquals(listOf("GET /mobile/v1/captures/$id/receipt"), server.requests)
        assertTrue(spool.exists("receipts", "$id.json"))
    }

    @Test fun aRejectedCaptureIsBlockedWithItsCodeAndDoesNotHoldBackTheOthers() {
        // FA1 has a wrong sha (the server answers 422 sha256_mismatch); FA0 is fine.
        server.serve("captures.receipt_before_upload", "captures.payload_wrong_sha", "captures.payload_staged", "captures.manifest_stored")
        val bad = Fx.wrongShaManifest
        server.serve("captures.receipt_before_upload")
        spoolCapture(bad); spoolCapture()
        val run = exchange().push()
        assertFalse("a rejected item needs the user, not a retry loop", run.retry)
        assertEquals("sha256_mismatch", run.errorCode)
        assertTrue(spool.exists("receipts", "$id.json"))
        assertTrue("rejected capture is kept, never dropped", spool.exists("outbox", "${bad.captureId}.json"))
        assertEquals(1, HttpExchange.queue(spool, state).blocked)
    }

    @Test fun aManifestConflictIsBlockedAndKept() {
        server.serve("captures.receipt_before_upload", "captures.payload_staged", "captures.manifest_conflict")
        spoolCapture(Fx.conflictingManifest)
        val run = exchange().push()
        assertFalse(run.retry); assertEquals("manifest_conflict", run.errorCode)
        assertTrue(spool.exists("outbox", "$id.json"))
        assertEquals(1, HttpExchange.queue(spool, state).blocked)
    }

    @Test fun manifestBeforePayloadIsRetryableNotBlocking() {
        server.serve("captures.receipt_before_upload", "captures.payload_staged", "captures.manifest_before_payload"); spoolCapture()
        val run = exchange().push()
        assertTrue(run.retry); assertEquals("payload_missing", run.errorCode)
        assertEquals(0, HttpExchange.queue(spool, state).blocked)
    }

    // ---- 403 classification (contract item 10) ----

    @Test fun nodeMismatchMeansPairAgainAndStopsTheRun() {
        spoolCapture(); server.failWith("/receipt", "errors.node_mismatch")
        val run = exchange().push()
        assertTrue(run.needsRepair); assertFalse(run.retry)
        assertTrue(spool.exists("outbox", "$id.json"))
    }

    @Test fun invalidAndRevokedTokensMeanPairAgain() {
        spoolCapture()
        listOf("errors.invalid_token", "errors.token_revoked").forEach { name ->
            server.clearFailures(); server.failWith("/receipt", name)
            assertTrue(name, HttpExchange(HttpsMobileApi(FakeMobileServer.pairing(server)), spool, state, allHttp).push().needsRepair)
        }
    }

    @Test fun scopeDeniedBlocksOnlyThatFeatureAndKeepsTheOthersRunning() {
        server.serve("reviews.list", "reviews.get_revision", "reviews.verdict_accepted", "reviews.operation_queued")
        exchange().pull()
        spoolVerdict(Fx.verdict, Fx.reviewEtag); spoolCapture()
        server.failWith("/receipt", "errors.scope_denied")
        val run = exchange().push()
        assertFalse("not a re-pair", run.needsRepair); assertFalse(run.retry)
        assertEquals("scope_denied", state.load().blockedFeatures["capture"])
        assertTrue("capture stays queued, nothing is dropped", spool.exists("outbox", "$id.json"))
        assertEquals("the review feature was not blocked", 1, server.count("/verdict"))
        server.clearFailures(); server.serve(*uploadFlow)
        exchange().push()
        assertEquals("the blocked feature does not retry by itself", 0, server.count("/payload"))
        state.update { it.copy(blockedFeatures = emptyMap()) }
        exchange().push()
        assertTrue(spool.exists("receipts", "$id.json"))
    }

    @Test fun notExecutorBlocksTheActionFeatureOnly() {
        server.failWith("/actions", "errors.not_executor")
        val run = exchange().pull()
        assertFalse(run.needsRepair)
        assertEquals("not_executor", state.load().blockedFeatures["action"])
    }

    @Test fun whoisUnavailableIs503AndRetried() {
        spoolCapture(); server.failWith("/receipt", "errors.whois_unavailable")
        val run = exchange().push()
        assertTrue(run.retry); assertFalse(run.needsRepair)
        assertTrue(state.load().blockedFeatures.isEmpty() && state.load().blocked.isEmpty())
    }

    @Test fun otherConfirmedForbiddenStopsTheRunWithoutAskingToPairAgain() {
        spoolCapture(); server.failWith("/receipt", "errors.peer_unverified")
        val run = exchange().push()
        assertFalse(run.needsRepair); assertFalse(run.retry)
        assertEquals("peer_unverified", run.errorCode)
    }

    @Test fun actionsThatTheServerStillSendsOverFilesAreNotAnError() {
        server.serve("actions_files.transport_files")
        val run = HttpExchange(HttpsMobileApi(FakeMobileServer.pairing(server)), spool, state,
            { if (it == TransportFeature.ACTION_RESULT) TransportRoute.HTTP else TransportRoute.FILES }).pull()
        assertFalse(run.retry); assertNull(run.errorCode); assertTrue(state.load().blocked.isEmpty())
    }

    // ---- reviews, verdicts, operations ----

    @Test fun pullStoresReviewPackagesAndTheirEtag() {
        server.serve("reviews.list", "reviews.get_revision")
        exchange().pull()
        assertTrue(spool.exists("proposals", "${Fx.pkg}.r1.json"))
        assertEquals(Fx.reviewEtag, state.load().etags["${Fx.pkg}.r1"])
        CourierClient.decoded<ReviewPackage>(spool.read("proposals", "${Fx.pkg}.r1.json").use { it.readBytes().decodeToString() })
    }

    @Test fun verdictIsAcceptedWithIfMatchThenKeptUntilTheOperationIsApplied() {
        server.serve("reviews.list", "reviews.get_revision", "reviews.verdict_accepted", "reviews.operation_queued", "reviews.operation_waiting", "reviews.operation_applied")
        exchange().pull(); spoolVerdict(Fx.verdict, Fx.reviewEtag)
        assertFalse(exchange().push().retry)
        assertEquals(1, server.count("/verdict"))
        assertTrue("202 is acceptance, not application: the verdict stays queued", spool.exists("decisions", vName))
        assertEquals(1, HttpExchange.queue(spool, state).verdicts)
        exchange().push() // queued, waiting with a code: still not applied
        assertTrue(spool.exists("decisions", vName))
        exchange().push()
        assertFalse(spool.exists("decisions", vName))
        assertEquals("APPLIED", state.load().verdictOutcomes[vName])
        assertEquals("no second PUT for the same operation", 1, server.count("/verdict"))
    }

    @Test fun lostAcceptanceResponseResendsTheSameVerdictAndGetsTheSameOperation() {
        server.serve("reviews.list", "reviews.get_revision", "reviews.verdict_accepted", "reviews.verdict_resent", "reviews.operation_queued")
        exchange().pull(); spoolVerdict(Fx.verdict, Fx.reviewEtag)
        server.dropResponse.add("/verdict")
        assertTrue(exchange().push().retry)
        assertTrue(state.load().verdictOps.isEmpty())
        exchange().push()
        assertEquals(2, server.count("/verdict"))
        assertEquals(listOf("op_00000000000000000000000000000001"), state.load().verdictOps.values.toList())
        assertNull("not stale: the server returned the operation it already created", state.load().verdictOutcomes[vName])
    }

    @Test fun anAnswerKeepsItsEtagWhenItsPackageLeftTheListSoALostResponseIsNeverMistakenForStale() {
        // pkg_b is no longer in the server's list, but its answer is still queued: its ETag must survive the pull.
        server.serve("reviews.list", "reviews.get_revision")
        spoolVerdict(Fx.staleVerdict, Fx.staleEtag)
        exchange().pull()
        assertEquals(Fx.staleEtag, state.load().etags["pkg_b.r1"])
        assertFalse(state.load().verdictOutcomes.containsKey("pkg_b.r1.json"))
        assertTrue(spool.exists("decisions", "pkg_b.r1.json"))
    }

    @Test fun aNewerRevisionAnswersStaleAndTheOldVerdictIsNeverAppliedToIt() {
        server.serve("reviews.verdict_stale_revision")
        spoolVerdict(Fx.staleVerdict, Fx.staleEtag)
        val run = exchange().push()
        assertFalse("stale is final, not a retry", run.retry)
        assertEquals("STALE", state.load().verdictOutcomes["pkg_b.r1.json"])
        assertFalse(spool.exists("decisions", "pkg_b.r1.json"))
    }

    @Test fun aDifferentVerdictForTheSameRevisionIsAConflict() {
        server.serve("reviews.verdict_conflict")
        spoolVerdict(Fx.conflictingVerdict, Fx.reviewEtag)
        exchange().push()
        assertEquals("CONFLICT", state.load().verdictOutcomes[vName])
    }

    @Test fun anOperationTheServerRejectedIsFinalAndNeverResent() {
        server.serve("reviews.verdict_accepted")
        server.serveDerived("reviews.operation_applied") { body ->
            kotlinx.serialization.json.JsonObject(body.jsonObject.toMutableMap().also {
                it["state"] = kotlinx.serialization.json.JsonPrimitive("rejected"); it["code"] = kotlinx.serialization.json.JsonPrimitive("stale_rev")
            })
        }
        spoolVerdict(Fx.verdict, Fx.reviewEtag)
        exchange().push()
        assertEquals("STALE", state.load().verdictOutcomes[vName])
        assertFalse(spool.exists("decisions", vName))
        assertEquals(1, server.count("/verdict"))
    }

    @Test fun withdrawnPackagesAreRemovedOnlyAfterACompleteListing() {
        server.serve("reviews.list", "reviews.get_revision")
        exchange().pull()
        spool.put("proposals", "pkg_gone.r1.json", "{}".toByteArray())
        server.failWith("/reviews", "errors.whois_unavailable")
        exchange().pull()
        assertTrue("a failed listing deletes nothing", spool.exists("proposals", "pkg_gone.r1.json"))
        server.clearFailures()
        exchange().pull()
        assertFalse(spool.exists("proposals", "pkg_gone.r1.json"))
        assertTrue(spool.exists("proposals", "${Fx.pkg}.r1.json"))
    }

    // ---- actions ----

    @Test fun actionResultIsKeptUntilTheServerAppliedItThenTheActionIsRetired() {
        server.serve("actions.list", "actions.result_accepted", "actions.operation_applied")
        exchange().pull()
        assertTrue(spool.exists("actions", "${Fx.actionId}.json"))
        CourierClient.decoded<Action>(spool.read("actions", "${Fx.actionId}.json").use { it.readBytes().decodeToString() })
        spool.publish("action-results", "${Fx.actionId}.json") { it.write(Fx.resultText.toByteArray()) }
        exchange().push()
        assertEquals(1, server.count("/result"))
        assertFalse("the first poll already says applied in this fixture order", spool.exists("action-results", "${Fx.actionId}.json"))
        assertFalse(spool.exists("actions", "${Fx.actionId}.json"))
        assertTrue(Fx.actionId in state.load().actionsDone)
        server.reset("actions.list"); server.serve("actions.list_after_finish")
        exchange().pull()
        assertFalse("an applied action is never downloaded again", spool.exists("actions", "${Fx.actionId}.json"))
    }

    @Test fun lostActionResultResponseIsResentWithoutADuplicate() {
        server.serve("actions.result_accepted", "actions.result_resent", "actions.operation_applied")
        spool.publish("action-results", "${Fx.actionId}.json") { it.write(Fx.resultText.toByteArray()) }
        server.dropResponse.add("/result")
        assertTrue(exchange().push().retry)
        exchange().push()
        assertEquals(2, server.count("/result"))
        assertFalse(spool.exists("action-results", "${Fx.actionId}.json"))
    }

    @Test fun conflictingActionResultIsBlockedNotRetried() {
        server.serve("actions.result_conflict")
        spool.publish("action-results", "${Fx.actionId}.json") { it.write(Fx.conflictingResultText.toByteArray()) }
        val run = exchange().push()
        assertFalse(run.retry)
        assertEquals(1, HttpExchange.queue(spool, state).blocked)
        assertTrue(spool.exists("action-results", "${Fx.actionId}.json"))
    }

    @Test fun alertsAndPipelineAreOnlyRequestedWhenTheServerOffersThem() {
        server.serve("reviews.list", "reviews.get_revision", "actions.list")
        exchange { false }.apply { pull(); push() }
        assertTrue(server.requests.none { it.contains("/alerts") || it.contains("/pipeline") })
        assertTrue(spool.list("proposals/pipeline").isEmpty())
    }

    @Test fun onlyTheFeaturesRoutedOverHttpTouchTheServer() {
        spoolCapture(); server.serve("reviews.list", "reviews.get_revision")
        val onlyReview = HttpExchange(HttpsMobileApi(FakeMobileServer.pairing(server)), spool, state,
            { if (it == TransportFeature.VERDICT) TransportRoute.HTTP else TransportRoute.FILES })
        onlyReview.pull(); onlyReview.push()
        assertTrue(server.requests.none { it.contains("captures") || it.contains("actions") })
        assertTrue(spool.exists("outbox", "$id.json"))
    }
}
