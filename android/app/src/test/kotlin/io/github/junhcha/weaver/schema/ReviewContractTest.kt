package io.github.junhcha.weaver.schema

import com.networknt.schema.InputFormat
import com.networknt.schema.SchemaRegistry
import com.networknt.schema.SchemaRegistryConfig
import com.networknt.schema.SpecificationVersion
import java.io.File
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
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

    @Test
    fun approvedScheduleRoundTripAndInvalidValues() {
        val value = ApprovedSchedule(
            2, "package-1", "thread-1", "회의", "2026-01-01T09:00:00+09:00", null,
            "Asia/Seoul", null, "a".repeat(64),
        )
        val text = WeaverCodec.encode(value)
        assertTrue(valid("ApprovedSchedule", text))
        assertTrue(WeaverCodec.decode<ApprovedSchedule>(text) is DecodeResult.Ok)
        assertTrue(runCatching { value.copy(timezone = "No/Such_Zone") }.isFailure)
        assertTrue(runCatching { value.copy(end = "2025-01-01T00:00:00Z") }.isFailure)
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
