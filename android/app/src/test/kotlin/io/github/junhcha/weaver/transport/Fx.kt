package io.github.junhcha.weaver.transport

import io.github.junhcha.weaver.courier.CourierClient
import io.github.junhcha.weaver.schema.*
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject

/** Values the contract fixtures agree on (ids, bodies), so tests send exactly what the fixtures expect. */
object Fx {
    val captureId = "cap_01ARZ3NDEKTSV4RRFFQ69G5FA0"
    val payloadText: String get() = ContractFixtures["captures.payload_staged"].requestText!!
    val manifestText: String get() = ContractFixtures["captures.manifest_stored"].requestText!!
    val manifest: Manifest get() = CourierClient.decoded(manifestText)
    /** Same capture, other `captured_at`: the server answers `manifest_conflict`. */
    val conflictingManifest: Manifest get() = CourierClient.decoded(ContractFixtures["captures.manifest_conflict"].requestText!!)
    /** `…FA1` with an all-zero sha: the server answers `sha256_mismatch` on its payload. */
    val wrongShaManifest: Manifest get() = manifest.copy(captureId = "cap_01ARZ3NDEKTSV4RRFFQ69G5FA1",
        payload = "cap_01ARZ3NDEKTSV4RRFFQ69G5FA1.txt", sha256 = "0".repeat(64))

    const val pkg = "pkg_a"
    val reviewEtag: String get() = ((ContractFixtures["reviews.list"].responseBody!!.jsonObject["items"] as JsonArray)[0].jsonObject["etag"] as JsonPrimitive).content
    val verdict: Verdict get() = CourierClient.decoded(ContractFixtures["reviews.verdict_accepted"].requestBody.toString())
    val conflictingVerdict: Verdict get() = CourierClient.decoded(ContractFixtures["reviews.verdict_conflict"].requestBody.toString())
    val staleVerdict: Verdict get() = CourierClient.decoded(ContractFixtures["reviews.verdict_stale_revision"].requestBody.toString())
    val staleEtag: String get() = ContractFixtures["reviews.verdict_stale_revision"].requestHeaders.getValue("if-match")

    const val actionId = "act_33e7c09122402846bba400b277ee6339"
    val actionText: String get() = (ContractFixtures["actions.list"].responseBody!!.jsonObject["items"] as JsonArray)[0].toString()
    val resultText: String get() = ContractFixtures["actions.result_accepted"].requestBody.toString()
    val conflictingResultText: String get() = ContractFixtures["actions.result_conflict"].requestBody.toString()

    fun json(name: String) = ContractFixtures[name].responseBody!!.jsonObject

    /** The thread pages of the fixture chain, in order (each page's cursor is the previous page's `next_cursor`). */
    val threadPages: List<String> = listOf("read.threads_first_page", "read.threads_next_page", "read.threads_last_page")
    /** How many threads the whole chain holds, derived from the fixture so regenerating it never breaks the tests. */
    val threadCount: Int get() = threadPages.sumOf { (json(it)["items"] as JsonArray).size }
}
