package io.github.junhcha.weaver.schema

import com.networknt.schema.InputFormat
import com.networknt.schema.SchemaRegistry
import com.networknt.schema.SchemaRegistryConfig
import com.networknt.schema.SpecificationVersion
import io.github.junhcha.weaver.schema.v2.Question
import io.github.junhcha.weaver.schema.v2.ReviewPackage
import io.github.junhcha.weaver.schema.v2.Stage
import io.github.junhcha.weaver.schema.v2.Verdict as VerdictV2
import java.io.File
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class V2ContractTest {
    private val json = Json { encodeDefaults = true }
    private val registry = SchemaRegistry.withDefaultDialect(SpecificationVersion.DRAFT_2020_12) {
        it.schemaRegistryConfig(SchemaRegistryConfig.builder().formatAssertionsEnabled(true).build())
    }
    private fun valid(model: String, text: String): Boolean = registry.getSchema(
        File(SharedSchema.dir, "v2/$model.schema.json").readText(), InputFormat.JSON,
    ).validate(text, InputFormat.JSON).isEmpty()

    @Test
    fun approvedScheduleRoundTripAndInvalidValues() {
        val value = io.github.junhcha.weaver.schema.v2.ApprovedSchedule(
            2, "package-1", "thread-1", "회의", "2026-01-01T09:00:00+09:00", null,
            "Asia/Seoul", null, "a".repeat(64),
        )
        val text = WeaverCodec.encode(value)
        assertTrue(valid("ApprovedSchedule", text))
        assertTrue(WeaverCodec.decode<io.github.junhcha.weaver.schema.v2.ApprovedSchedule>(text) is DecodeResult.Ok)
        assertTrue(runCatching { value.copy(timezone = "No/Such_Zone") }.isFailure)
        assertTrue(runCatching { value.copy(end = "2025-01-01T00:00:00Z") }.isFailure)
    }

    @Test
    fun courierCodecAcceptsOnlyStrictV2ForV2Models() {
        val value = VerdictV2(2, "package-1", 1, VerdictV2.Action.APPROVE, listOf("가"), null, null)
        val text = WeaverCodec.encode(value)
        assertTrue(WeaverCodec.decode<VerdictV2>(text) is DecodeResult.Ok)
        assertTrue(WeaverCodec.decode<VerdictV2>(text.replace("\"schema_version\":2", "\"schema_version\":1")) is DecodeResult.UnsupportedVersion)
        assertTrue(WeaverCodec.decode<VerdictV2>(text.replace("\"revision\":1", "\"revision\":\"1\"")) is DecodeResult.Invalid)
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
        val base = VerdictV2(2, "package-1", 1, VerdictV2.Action.APPROVE, emptyList(), null, null)
        listOf(base, base.copy(action = VerdictV2.Action.EDIT, editedBody = "수정"), base.copy(action = VerdictV2.Action.FEEDBACK, feedback = "의견"), base.copy(action = VerdictV2.Action.REJECT)).forEach {
            val text = json.encodeToString(it)
            assertTrue(valid("Verdict", text))
            assertTrue(json.decodeFromString<VerdictV2>(text) == it)
        }
        val invalid = json.encodeToString(base).replace("\"action\":\"approve\"", "\"action\":\"edit\"")
        assertFalse(valid("Verdict", invalid))
        assertTrue(runCatching { json.decodeFromString<VerdictV2>(invalid) }.isFailure)
    }
}
