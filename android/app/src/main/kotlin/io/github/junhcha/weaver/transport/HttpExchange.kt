package io.github.junhcha.weaver.transport

import io.github.junhcha.weaver.courier.CourierClient
import io.github.junhcha.weaver.courier.CourierRules
import io.github.junhcha.weaver.schema.Manifest
import io.github.junhcha.weaver.schema.Receipt
import io.github.junhcha.weaver.schema.Verdict
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject

/** What a run learned, as stable codes only. [retry] asks WorkManager for a backoff; blocked items never do. */
class ExchangeSummary {
    var retry = false
    var unreachable = false
    var needsRepair = false
    var errorCode: String? = null
    /** Items the server refused for good with a code the person must be able to see: item key to code. */
    val rejected = linkedMapOf<String, String>()
    internal fun fail(code: String, retry: Boolean) { errorCode = errorCode ?: code; if (retry) this.retry = true }
}

data class QueueCounts(val captures: Int, val verdicts: Int, val results: Int, val blocked: Int) {
    val total: Int get() = captures + verdicts + results
}

/**
 * Moves data between the HTTP [spool] and the server. Pull fills `proposals/`, `actions/` and
 * `proposals/pipeline/`; push drains `outbox/`, `decisions/` and `action-results/`. Every spooled item keeps its
 * Courier ID and stays until the server's own confirmation (Receipt, operation `applied`), and each item fails
 * on its own: one conflict never holds back the rest. Called under the Courier process lock on an IO thread.
 */
class HttpExchange(
    private val api: MobileApi,
    private val spool: SpoolFolder,
    private val state: TransportState,
    private val route: (TransportFeature) -> TransportRoute,
    private val offered: (ServerFeature) -> Boolean = { false },
) {
    val summary = ExchangeSummary()
    private class Abort : RuntimeException()
    private val featureDenials = setOf("scope_denied", "not_executor")

    private fun http(feature: TransportFeature) = route(feature) == TransportRoute.HTTP

    fun pull(): ExchangeSummary = guarded {
        if (http(TransportFeature.VERDICT)) {
            item(TransportFeature.VERDICT, "list:reviews", ::pullReviews)
            // Alerts and the map are read over HTTP only while the server offers them; otherwise the Syncthing folder keeps feeding them.
            if (offered(ServerFeature.ALERTS)) item(TransportFeature.VERDICT, "list:alerts", ::pullAlerts)
            if (offered(ServerFeature.PIPELINE)) item(TransportFeature.VERDICT, "list:pipeline", ::pullPipeline)
        }
        if (http(TransportFeature.ACTION_RESULT)) item(TransportFeature.ACTION_RESULT, "list:actions", ::pullActions)
    }

    fun push(): ExchangeSummary = guarded {
        if (http(TransportFeature.CAPTURE)) pushCaptures()
        if (http(TransportFeature.VERDICT)) { pushVerdicts(); if (offered(ServerFeature.ALERTS)) pushAlertAcks() }
        if (http(TransportFeature.ACTION_RESULT)) pushResults()
    }

    private fun guarded(block: () -> Unit): ExchangeSummary {
        try { block() } catch (_: Abort) { }
        return summary
    }

    /**
     * Runs one item of [feature]. Errors are classified by the server's fixed codes: only 401 and `node_mismatch` mean
     * "pair again" (and stop the run); `scope_denied` / `not_executor` block that feature alone; other confirmed 403s stop
     * the run without asking for a new pairing; 409/412/413/422 block the item; 429/5xx (including 503 `whois_unavailable`)
     * and lost connections are retried with backoff.
     */
    private fun item(feature: TransportFeature, key: String, block: () -> Unit) {
        val data = state.load()
        if (key in data.blocked || feature.wire in data.blockedFeatures) return
        try { block() }
        catch (error: ApiException) {
            when {
                error.status == 401 || error.code == "node_mismatch" -> { summary.needsRepair = true; summary.fail(error.code, false); throw Abort() }
                error.status == 403 && error.code in featureDenials -> {
                    state.update { it.copy(blockedFeatures = it.blockedFeatures + (feature.wire to error.code)) }
                    summary.fail(error.code, false)
                }
                error.status == 403 -> { summary.fail(error.code, false); throw Abort() }
                error.status == 409 && error.code == "transport_files" -> Unit // the server switched this feature back; the next capabilities read follows it
                error.retryable -> summary.fail(error.code, true)
                else -> {
                    blockItem(key, error.code)
                    if (error.status == 422 && error.code == CALL_TIME_REQUIRED) summary.rejected[key] = error.code
                    summary.fail(error.code, false)
                }
            }
        } catch (error: PinMismatchException) { summary.needsRepair = true; summary.fail("pin_mismatch", false); throw Abort()
        } catch (error: InvalidResponseException) { summary.fail("invalid_read_response", false); throw Abort()
        } catch (error: UnreachableException) { summary.unreachable = true; summary.fail("unreachable", true); throw Abort()
        } catch (error: SecurityException) { throw error
        } catch (error: Exception) { blockItem(key, CourierClient.errorCode(error)); summary.fail(CourierClient.errorCode(error), false) }
    }

    private fun blockItem(key: String, code: String) { state.update { it.copy(blocked = it.blocked + (key to code)) } }

    // ---- pull ----

    private fun pullReviews() {
        val refs = api.reviews()
        val wanted = refs.map { "${it.packageId}.r${it.revision}" }.toSet()
        refs.forEach { ref ->
            val key = "${ref.packageId}.r${ref.revision}"
            item(TransportFeature.VERDICT, "review:$key") {
                if (!spool.exists("proposals", "$key.json")) {
                    val doc = api.review(ref.packageId, ref.revision)
                    val etag = doc.etag ?: ref.etag ?: throw ApiException(200, "etag_missing", false)
                    // The ETag is remembered first: a package without its ETag could never be answered.
                    state.update { it.copy(etags = it.etags + (key to etag)) }
                    spool.put("proposals", "$key.json", doc.body)
                }
            }
        }
        spool.list("proposals").filter { it.regular }.forEach { entry ->
            val match = CourierRules.revision.matchEntire(entry.name) ?: return@forEach
            if ("${match.groupValues[1]}.r${match.groupValues[2]}" !in wanted) spool.delete("proposals", entry.name)
        }
        // An answer still waiting for the server keeps its ETag even after its package left the list: when the first
        // response was lost and the server already applied it, the same-key resend with the original ETag returns the
        // same operation. Dropping the ETag would make that answer look stale.
        state.update { it.copy(etags = it.etags.filterKeys { key -> key in wanted || spool.exists("decisions", "$key.json") }) }
    }

    private fun pullAlerts() {
        val listed = api.alerts().mapNotNull { (it as? JsonObject)?.let { o -> id(o, "alert_id") to it } }
        val ids = listed.map { it.first }.toSet()
        listed.forEach { (id, element) ->
            item(TransportFeature.VERDICT, "alert:$id") {
                if (id !in state.load().alertsSeen) {
                    spool.put("proposals", "alert_$id.json", element.toString().toByteArray())
                    state.update { it.copy(alertsSeen = it.alertsSeen + id) }
                }
            }
        }
        // Withdrawn on the server: the inbox treats the missing file as removal.
        spool.list("proposals").filter { it.regular }.forEach { entry ->
            val match = CourierRules.alert.matchEntire(entry.name) ?: return@forEach
            if (match.groupValues[1] !in ids) spool.delete("proposals", entry.name)
        }
        state.update { it.copy(alertsSeen = it.alertsSeen.filter { id -> id in ids || spool.exists("proposals", "alert_$id.json") }.toSet()) }
    }

    private fun pullPipeline() {
        val items = Paging.all(HttpsMobileApi.PAGE, api::pipeline)
        val names = mutableSetOf<String>()
        items.forEach { element ->
            val id = (element as? JsonObject)?.let { id(it, "source_id") } ?: return@forEach
            names += "$id.json"
            item(TransportFeature.VERDICT, "pipeline:$id") {
                val bytes = element.toString().toByteArray()
                if (!spool.exists("proposals/pipeline", "$id.json") ||
                    !spool.read("proposals/pipeline", "$id.json").use { it.readBytes() }.contentEquals(bytes)) {
                    spool.put("proposals/pipeline", "$id.json", bytes)
                }
            }
        }
        // Only a complete listing reaches this point; a failed page threw above and deleted nothing.
        spool.list("proposals/pipeline").filter { it.regular && !CourierRules.ignored(it.name) && it.name !in names }
            .forEach { spool.delete("proposals/pipeline", it.name) }
    }

    private fun pullActions() {
        val listed = api.actions().mapNotNull { (it as? JsonObject)?.let { o -> id(o, "action_id") to it } }
        val done = state.load().actionsDone
        val data = state.load()
        listed.forEach { (id, element) ->
            if (id in done) return@forEach
            item(TransportFeature.ACTION_RESULT, "action:$id") {
                if (!spool.exists(CourierRules.ACTIONS, "$id.json")) spool.put(CourierRules.ACTIONS, "$id.json", element.toString().toByteArray())
            }
        }
        val ids = listed.map { it.first }.toSet()
        spool.list(CourierRules.ACTIONS).filter { it.regular && !CourierRules.ignored(it.name) }.forEach { entry ->
            val id = entry.name.removeSuffix(".json")
            if (id !in ids && id !in data.resultOps && !spool.exists(CourierRules.ACTION_RESULTS, entry.name)) {
                spool.delete(CourierRules.ACTIONS, entry.name)
            }
        }
        state.update { it.copy(actionsDone = it.actionsDone.filter { id -> id in ids }.toSet()) }
    }

    // ---- push ----

    private fun pushCaptures() {
        spool.list("outbox").filter { it.regular && CourierRules.capture.matches(it.name) }.forEach { entry ->
            val id = entry.name.removeSuffix(".json")
            item(TransportFeature.CAPTURE, "capture:$id") {
                val bytes = spool.read("outbox", entry.name).use { it.readBytes() }
                val manifest = CourierClient.decoded<Manifest>(bytes.decodeToString())
                check(manifest.captureId == id) { "id_mismatch" }
                val payload = spool.fileOf("outbox", manifest.payload)
                // After a lost response the Receipt may already exist: look before sending the body again.
                val receipt = api.receipt(id) ?: run {
                    check(payload.isFile) { "payload_missing" }
                    api.putPayload(id, payload, manifest.sha256)
                    api.putManifest(id, bytes)
                }
                val decoded = CourierClient.decoded<Receipt>(receipt.decodeToString())
                check(decoded.captureId == id && decoded.sha256 == manifest.sha256) { "hash_mismatch" }
                // The Receipt is on disk before the local copy goes: it is the only proof the archive has it.
                spool.put("receipts", entry.name, receipt)
                spool.delete("outbox", manifest.payload)
                spool.delete("outbox", entry.name)
            }
        }
    }

    private fun pushVerdicts() {
        spool.list("decisions").filter { it.regular && !CourierRules.ignored(it.name) }.forEach { entry ->
            val match = CourierRules.revision.matchEntire(entry.name) ?: return@forEach
            val name = entry.name
            item(TransportFeature.VERDICT, "verdict:$name") {
                val ops = state.load().verdictOps
                val known = ops[name]
                if (known != null) {
                    verdictProgress(name, known)
                } else {
                    val key = "${match.groupValues[1]}.r${match.groupValues[2]}"
                    val etag = state.load().etags[key]
                    if (etag == null) {
                        // The package this answer is for is gone (a newer revision replaced it, or it was withdrawn):
                        // the answer can never be applied. The ReviewStore keeps the person's text.
                        if (!spool.exists("proposals", "$key.json")) {
                            state.update { it.copy(verdictOutcomes = it.verdictOutcomes + (name to "STALE")) }
                            spool.delete("decisions", name)
                        }
                        return@item
                    }
                    val bytes = spool.read("decisions", name).use { it.readBytes() }
                    CourierClient.decoded<Verdict>(bytes.decodeToString())
                    val operation = try { api.putVerdict(match.groupValues[1], match.groupValues[2].toLong(), bytes, etag) }
                    catch (error: ApiException) {
                        // A newer revision or a different verdict for this revision: the decision is final and the
                        // ReviewStore keeps the person's text, so the spooled copy is dropped and nothing retries.
                        val outcome = when (error.status) { 412, 404, 410 -> "STALE"; 409 -> "CONFLICT"; else -> null }
                        if (outcome == null) throw error
                        state.update { it.copy(verdictOutcomes = it.verdictOutcomes + (name to outcome)) }
                        spool.delete("decisions", name)
                        summary.fail(error.code, false)
                        return@item
                    }
                    state.update { it.copy(verdictOps = it.verdictOps + (name to operation)) }
                    verdictProgress(name, operation)
                }
            }
        }
    }

    private fun verdictProgress(name: String, operationId: String) {
        val operation = try { api.operation(operationId) } catch (error: ApiException) {
            // An unknown operation was lost on the server: the same key and bytes are sent again.
            if (error.status == 404) { state.update { it.copy(verdictOps = it.verdictOps - name) }; return }
            throw error
        }
        when (operation.state) {
            "applied" -> {
                state.update { it.copy(verdictOps = it.verdictOps - name, verdictOutcomes = it.verdictOutcomes + (name to "APPLIED")) }
                spool.delete("decisions", name)
            }
            // The domain refused (stale revision, conflict): final. The ReviewStore keeps the person's text.
            "rejected" -> {
                val outcome = if (operation.code?.startsWith("stale") == true) "STALE" else "CONFLICT"
                state.update { it.copy(verdictOps = it.verdictOps - name, verdictOutcomes = it.verdictOutcomes + (name to outcome)) }
                spool.delete("decisions", name)
            }
            "failed" -> {
                state.update { it.copy(verdictOutcomes = it.verdictOutcomes + (name to "ERROR")) }
                blockItem("verdict:$name", operation.code ?: "apply_failed")
            }
            // queued: accepted but waiting (the code says why); keep polling.
        }
    }

    private fun pushResults() {
        spool.list(CourierRules.ACTION_RESULTS).filter { it.regular && !CourierRules.ignored(it.name) }.forEach { entry ->
            val id = entry.name.removeSuffix(".json")
            item(TransportFeature.ACTION_RESULT, "result:$id") {
                val operation = state.load().resultOps[id] ?: api.putActionResult(
                    id, spool.read(CourierRules.ACTION_RESULTS, entry.name).use { it.readBytes() },
                ).also { op -> state.update { it.copy(resultOps = it.resultOps + (id to op)) } }
                val current = try { api.operation(operation) } catch (error: ApiException) {
                    if (error.status == 404) { state.update { it.copy(resultOps = it.resultOps - id) }; return@item }
                    throw error
                }
                when (current.state) {
                    "applied" -> {
                        // Only now may the phone forget the result and stop offering the action.
                        state.update { it.copy(resultOps = it.resultOps - id, actionsDone = it.actionsDone + id) }
                        spool.delete(CourierRules.ACTION_RESULTS, entry.name)
                        spool.delete(CourierRules.ACTIONS, entry.name)
                    }
                    "failed", "rejected" -> blockItem("result:$id", current.code ?: "apply_failed")
                }
            }
        }
    }

    private fun pushAlertAcks() {
        state.load().alertsSeen.forEach { id ->
            if (!spool.exists("proposals", "alert_$id.json")) item(TransportFeature.VERDICT, "ack:$id") { api.ackAlert(id) }
        }
    }

    private fun id(o: JsonObject, key: String): String =
        (o[key] as? JsonPrimitive)?.contentOrNull?.takeIf { it.matches(Regex("[A-Za-z0-9_-]+")) } ?: throw ApiException(200, "invalid_item", false)

    companion object {
        const val CALL_TIME_REQUIRED = "call_occurred_at_required"

        fun queue(spool: SpoolFolder, state: TransportState): QueueCounts {
            val receipts = spool.list("receipts").map { it.name }.toSet()
            return QueueCounts(
                spool.list("outbox").count { it.regular && CourierRules.capture.matches(it.name) && it.name !in receipts },
                spool.list("decisions").count { it.regular && CourierRules.revision.matches(it.name) },
                spool.list(CourierRules.ACTION_RESULTS).count { it.regular && !CourierRules.ignored(it.name) },
                state.load().blocked.size,
            )
        }
    }
}
