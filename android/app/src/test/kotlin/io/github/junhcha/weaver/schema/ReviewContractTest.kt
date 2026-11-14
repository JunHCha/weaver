package io.github.junhcha.weaver.schema

import com.networknt.schema.InputFormat
import com.networknt.schema.SchemaRegistry
import com.networknt.schema.SchemaRegistryConfig
import com.networknt.schema.SpecificationVersion
import java.io.File
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ReviewContractTest {
    private val json = Json { encodeDefaults = true }
    private val registry = SchemaRegistry.withDefaultDialect(SpecificationVersion.DRAFT_2020_12) {
        it.schemaRegistryConfig(SchemaRegistryConfig.builder().formatAssertionsEnabled(true).build())
    }
    private fun valid(model: String, text: String): Boolean = registry.getSchema(
        File(SharedSchema.dir, "$model.schema.json").readText(), InputFormat.JSON,
    ).validate(text, InputFormat.JSON).isEmpty()

    @Test
    fun noVersionDirectoryRemainsAndEveryModelHasAnExample() {
        assertFalse(File(SharedSchema.dir, "v2").exists())
        SharedSchema.models.keys.forEach { assertTrue(it, File(SharedSchema.dir, "examples/$it.json").isFile) }
    }

    private val payload = CalendarPayload("2026-01-01T09:00:00+09:00", null, "Asia/Seoul", "회의")
    private val actionId = "act_0123456789abcdef0123456789abcdef"

    @Test
    fun actionRoundTripAndInvalidValues() {
        val register = Action(2, actionId, ActionKind.REGISTER, "thread-1", 1, 1, payload, null)
        val update = register.copy(kind = ActionKind.UPDATE, externalId = "content://com.android.calendar/events/7")
        listOf(register, update, update.copy(kind = ActionKind.REMOVE)).forEach {
            val text = WeaverCodec.encode(it)
            assertTrue(valid("Action", text))
            assertEquals(DecodeResult.Ok(it), WeaverCodec.decode<Action>(text))
        }
        // The schema and the model agree that only register has no target event yet.
        assertFalse(valid("Action", WeaverCodec.encode(update).replace("\"kind\":\"calendar.update\"", "\"kind\":\"calendar.register\"")))
        assertFalse(valid("Action", WeaverCodec.encode(register).replace("\"external_id\":null", "\"external_id\":\"x\"")))
        listOf(
            { register.copy(externalId = "x") }, { update.copy(externalId = null) }, { register.copy(actionId = "act_zz") },
            { register.copy(seq = 0) }, { register.copy(payload = payload.copy(timezone = "No/Such_Zone")) },
            { register.copy(payload = payload.copy(end = "2025-01-01T00:00:00Z")) }, { register.copy(payload = payload.copy(title = " ")) },
        ).forEach { assertTrue(runCatching { it() }.isFailure) }
    }

    @Test
    fun actionResultRoundTripAndInvalidValues() {
        val done = ActionResult(2, actionId, ActionResult.Outcome.COMPLETED, "content://com.android.calendar/events/7", 1, "2026-01-01T00:00:00Z", null)
        val failed = ActionResult(2, actionId, ActionResult.Outcome.FAILED, null, 5, "2026-01-01T00:00:00Z", "provider_error")
        listOf(done, failed).forEach {
            val text = WeaverCodec.encode(it)
            assertTrue(valid("ActionResult", text))
            assertEquals(DecodeResult.Ok(it), WeaverCodec.decode<ActionResult>(text))
        }
        assertFalse(valid("ActionResult", WeaverCodec.encode(done).replace("\"external_id\":\"content://com.android.calendar/events/7\"", "\"external_id\":null")))
        assertFalse(valid("ActionResult", WeaverCodec.encode(failed).replace("\"error\":\"provider_error\"", "\"error\":null")))
        listOf(
            { done.copy(externalId = null) }, { done.copy(error = "x") }, { failed.copy(externalId = "x") },
            { failed.copy(error = null) }, { done.copy(attempts = 0) }, { done.copy(finishedAt = "2026-01-01T00:00:00") },
        ).forEach { assertTrue(runCatching { it() }.isFailure) }
    }

    @Test
    fun courierCodecAcceptsOnlyStrictReviewVersion() {
        val value = Verdict(2, "package-1", 1, Verdict.Action.APPROVE, listOf("가"), null, null)
        val text = WeaverCodec.encode(value)
        assertTrue(WeaverCodec.decode<Verdict>(text) is DecodeResult.Ok)
        assertTrue(WeaverCodec.decode<Verdict>(text.replace("\"schema_version\":2", "\"schema_version\":1")) is DecodeResult.UnsupportedVersion)
        assertTrue(WeaverCodec.decode<Verdict>(text.replace("\"revision\":1", "\"revision\":\"1\"")) is DecodeResult.Invalid)
    }

    @Test
    fun reviewPackageRoundTripAndQuestionBounds() {
        val question = Question("질문", "근거", listOf("가", "나", "다"))
        val value = ReviewPackage(2, "package-1", "src_01ARZ3NDEKTSV4RRFFQ69G5FAV", 1, "요약", listOf(question), Stage.REVIEW, mapOf(Stage.REVIEW to "2026-01-01T00:00:00Z"))
        val text = json.encodeToString(value)
        assertTrue(valid("ReviewPackage", text))
        assertTrue(json.decodeFromString<ReviewPackage>(text) == value)
        val tooMany = text.replace("[\"가\",\"나\",\"다\"]", "[\"가\",\"나\"]")
        assertFalse(valid("ReviewPackage", tooMany))
        assertTrue(runCatching { json.decodeFromString<ReviewPackage>(tooMany) }.isFailure)
        val four = text.replace("[${json.encodeToString(question)}]", "[${List(4) { json.encodeToString(question) }.joinToString(",")}]")
        assertFalse(valid("ReviewPackage", four))
        assertTrue(runCatching { json.decodeFromString<ReviewPackage>(four) }.isFailure)
    }

    @Test
    fun verdictActionsRoundTripAndRejectInvalidBranches() {
        val base = Verdict(2, "package-1", 1, Verdict.Action.APPROVE, emptyList(), null, null)
        listOf(base, base.copy(action = Verdict.Action.EDIT, editedBody = "수정"), base.copy(feedback = "의견"), base.copy(action = Verdict.Action.REJECT)).forEach {
            val text = json.encodeToString(it)
            assertTrue(valid("Verdict", text))
            assertTrue(json.decodeFromString<Verdict>(text) == it)
        }
        val invalid = json.encodeToString(base).replace("\"action\":\"approve\"", "\"action\":\"edit\"")
        assertFalse(valid("Verdict", invalid))
        assertTrue(runCatching { json.decodeFromString<Verdict>(invalid) }.isFailure)
    }
}
