package io.github.junhcha.weaver.schema

import java.io.File
import kotlinx.serialization.KSerializer
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * Contract between the Kotlin models and the repository-root schema/ v1 files.
 * The JSON Schema validator is the referee: Kotlin must accept what the schema accepts in
 * the shared examples, reject the invalid fixtures the schema rejects, and every JSON the
 * Kotlin models write must validate against the same schema.
 */
class SchemaContractTest {
    @Test
    fun examplesDecodeAndReencodeToSchemaValidIdenticalJson() {
        SharedSchema.models.forEach { (model, serializer) ->
            SharedSchema.examples(model).forEach { file ->
                val text = file.readText()
                assertEquals("${file.name} must satisfy its schema", emptyList<String>(), SharedSchema.errors(model, text))
                val value = decodeOk(serializer, text, file.name)
                val encoded = encode(serializer, value)
                assertEquals(
                    "${file.name}: Kotlin output must satisfy $model.schema.json",
                    emptyList<String>(),
                    SharedSchema.errors(model, encoded),
                )
                assertEquals("${file.name}: round trip", Json.parseToJsonElement(text), Json.parseToJsonElement(encoded))
            }
        }
    }

    @Test
    fun removingAnyRequiredKeyFailsSchemaAndKotlin() {
        SharedSchema.models.forEach { (model, serializer) ->
            SharedSchema.examples(model).forEach { file ->
                val example = Json.parseToJsonElement(file.readText())
                objectKeyPaths(example).forEach { path ->
                    val mutated = removeAt(example, path).toString()
                    val where = "${file.name} without ${path.joinToString("/")}"
                    assertFalse("$where must fail the schema", SharedSchema.errors(model, mutated).isEmpty())
                    assertNotOk(where, WeaverCodec.decode(serializer, mutated))
                }
            }
        }
    }

    @Test
    fun unknownVersionsAreSkippedWithDiagnostic() {
        SharedSchema.models.forEach { (model, serializer) ->
            val example = Json.parseToJsonElement(SharedSchema.examples(model).first().readText()).jsonObject
            listOf(0L, 2L, 99L).forEach { version ->
                val text = JsonObject(example + ("schema_version" to JsonPrimitive(version))).toString()
                assertFalse("$model v$version must fail the schema", SharedSchema.errors(model, text).isEmpty())
                val result = WeaverCodec.decode(serializer, text)
                assertEquals("$model v$version", DecodeResult.UnsupportedVersion(model, version), result)
                assertTrue((result as DecodeResult.UnsupportedVersion).diagnostic.contains("schema_version $version"))
            }
        }
    }

    @Test
    fun integerFieldsRequireIntegerTokens() {
        // JSON Schema treats 1.0 and 1e0 as integers; the README makes this lexical rule the
        // consumer's job, so only the Kotlin side is asserted for those tokens.
        listOf(
            Triple("Proposal", "\"schema_version\": 1", "\"schema_version\": 1.0"),
            Triple("Proposal", "\"schema_version\": 1", "\"schema_version\": 1e0"),
            Triple("Proposal", "\"rev\": 1", "\"rev\": 1.0"),
            Triple("Proposal", "\"rev\": 1", "\"rev\": 1e0"),
            Triple("Verdict", "\"rev\": 1", "\"rev\": 1e0"),
            Triple("Verdict", "\"rev\": 1", "\"rev\": 1E+0"),
            Triple("Manifest", "\"size\": 45", "\"size\": 45.0"),
            Triple("Manifest", "\"size\": 45", "\"size\": 4.5e1"),
        ).forEach { (model, from, to) -> assertKotlinInvalid(model, from, to) }
    }

    @Test
    fun quotedNumbersFailSchemaAndKotlin() {
        listOf(
            Triple("Proposal", "\"schema_version\": 1", "\"schema_version\": \"1\""),
            Triple("Proposal", "\"rev\": 1", "\"rev\": \"1\""),
            Triple("Verdict", "\"rev\": 1", "\"rev\": \"1\""),
            Triple("Manifest", "\"size\": 45", "\"size\": \"45\""),
            Triple("Proposal", "\"confidence\": 0.92", "\"confidence\": \"0.92\""),
        ).forEach { (model, from, to) ->
            val text = assertKotlinInvalid(model, from, to)
            assertFalse("$model $to must fail the schema", SharedSchema.errors(model, text).isEmpty())
        }
    }

    @Test
    fun largeRevisionsAreAccepted() {
        // rev is unbounded in the schema; Kotlin keeps it as Long.
        val verdict = SharedSchema.examples("Verdict").first().readText()
            .replaceFirst("\"rev\": 1", "\"rev\": 2147483648")
        assertEquals(emptyList<String>(), SharedSchema.errors("Verdict", verdict))
        val result = WeaverCodec.decode<Verdict>(verdict)
        assertEquals(2_147_483_648L, (result as DecodeResult.Ok).value.rev)
    }

    @Test
    fun appIsStricterThanSchemaForUnrealisticTimestamps() {
        // Documented in Primitives.kt: java.time rejects offsets beyond ±18:00 and more than
        // nine fraction digits, which no v1 producer writes. Pinned so the gap stays known.
        val receipt = SharedSchema.examples("Receipt").first().readText()
        listOf("+19:00", ".1234567890+09:00").forEach { suffix ->
            val text = receipt.replace("15:32:00+09:00", "15:32:00$suffix")
            assertTrue("fixture was not mutated", text != receipt)
            assertEquals(emptyList<String>(), SharedSchema.errors("Receipt", text))
            assertTrue(WeaverCodec.decode<Receipt>(text) is DecodeResult.Invalid)
        }
    }

    @Test
    fun lineBreaksAreRejectedLikeEcmaRegex() {
        val alert = SharedSchema.examples("Alert").first().readText()
        val text = alert.replace("\"schedule_ref\": \"s1\"", "\"schedule_ref\": \"s1\\n\"")
        assertTrue("fixture was not mutated", text != alert)
        assertFalse(SharedSchema.errors("Alert", text).isEmpty())
        assertTrue(WeaverCodec.decode<Alert>(text) is DecodeResult.Invalid)
    }

    private fun assertKotlinInvalid(model: String, from: String, to: String): String {
        val example = SharedSchema.examples(model).first().readText()
        val text = example.replaceFirst(from, to)
        assertTrue("$model fixture was not mutated by $to", text != example)
        val result = WeaverCodec.decode(SharedSchema.models.getValue(model), text)
        assertTrue("$model $to: $result", result is DecodeResult.Invalid)
        return text
    }

    @Test
    fun unknownKeysAreRejectedAtEveryLevel() {
        SharedSchema.models.forEach { (model, serializer) ->
            SharedSchema.examples(model).forEach { file ->
                val example = Json.parseToJsonElement(file.readText())
                objectPaths(example).forEach { path ->
                    val mutated = addAt(example, path, "unexpected_key").toString()
                    val where = "${file.name} with unexpected_key at /${path.joinToString("/")}"
                    assertFalse("$where must fail the schema", SharedSchema.errors(model, mutated).isEmpty())
                    assertNotOk(where, WeaverCodec.decode(serializer, mutated))
                }
            }
        }
    }

    /** test resources fixtures/invalid/<Model>.<case>.json: hand-written failure cases. */
    @Test
    fun invalidFixturesFailSchemaAndKotlin() {
        val dir = File(requireNotNull(javaClass.classLoader!!.getResource("fixtures/invalid")).toURI())
        val fixtures = dir.listFiles()!!.sortedBy { it.name }
        assertTrue("expected invalid fixtures", fixtures.size >= 5)
        fixtures.forEach { file ->
            val model = file.name.substringBefore('.')
            val serializer = SharedSchema.models[model] ?: return@forEach fail("unknown model in ${file.name}")
            val text = file.readText()
            assertFalse("${file.name} must fail the schema", SharedSchema.errors(model, text).isEmpty())
            val result = WeaverCodec.decode(serializer, text)
            if (file.name.contains(".unknown-version.")) {
                assertTrue("${file.name}: $result", result is DecodeResult.UnsupportedVersion)
            } else {
                assertTrue("${file.name}: $result", result is DecodeResult.Invalid)
            }
        }
    }

    @Test
    fun kotlinBuiltFilesSatisfySchema() {
        val manifest = Manifest(
            schemaVersion = SCHEMA_VERSION,
            captureId = "cap_01JC3V8Z9X2N4P6Q8R0S2T4V6W",
            kind = CaptureKind.FLEECE,
            channel = Channel.CALL,
            payload = "cap_01JC3V8Z9X2N4P6Q8R0S2T4V6W.m4a",
            sha256 = "0".repeat(64),
            size = 1_024,
            capturedAt = "2026-11-07T09:00:00Z",
            occurredAt = null,
            hints = Manifest.Hints(counterpart = null),
        )
        assertEquals(emptyList<String>(), SharedSchema.errors("Manifest", WeaverCodec.encode(manifest)))

        val base = Verdict(
            schemaVersion = SCHEMA_VERSION,
            proposalId = "prp_01JC3V8Z9X2N4P6Q8R0S2T4V6W",
            rev = 2,
            verdict = Verdict.Kind.APPROVE,
            edits = emptyList(),
            rejectReason = null,
            instruction = null,
            reviewedAt = "2026-11-07T15:32:00.123+09:00",
        )
        listOf(
            base,
            base.copy(verdict = Verdict.Kind.EDIT, edits = listOf(Verdict.Edit("/schedules/0/title", JsonNull))),
            base.copy(verdict = Verdict.Kind.REVISE, instruction = "다시 정리"),
            base.copy(verdict = Verdict.Kind.REJECT, rejectReason = Verdict.RejectReason.DUPLICATE),
        ).forEach {
            assertEquals(it.verdict.name, emptyList<String>(), SharedSchema.errors("Verdict", WeaverCodec.encode(it)))
        }
    }

    @Test
    fun kotlinRefusesToBuildMismatchedVerdictBranches() {
        val approve = Verdict(
            SCHEMA_VERSION, "prp_01JC3V8Z9X2N4P6Q8R0S2T4V6W", 1, Verdict.Kind.APPROVE,
            emptyList(), null, null, "2026-11-07T15:32:00+09:00",
        )
        listOf(
            { approve.copy(instruction = "x") },
            { approve.copy(verdict = Verdict.Kind.EDIT) },
            { approve.copy(verdict = Verdict.Kind.REVISE) },
            { approve.copy(verdict = Verdict.Kind.REJECT) },
            { approve.copy(rev = 0) },
            { approve.copy(reviewedAt = "2026-11-07 15:32:00") },
        ).forEach { build ->
            try {
                build()
                fail("expected IllegalArgumentException")
            } catch (_: IllegalArgumentException) {
            }
        }
    }

    @Suppress("UNCHECKED_CAST")
    private fun encode(serializer: KSerializer<*>, value: Any?): String =
        WeaverCodec.json.encodeToString(serializer as KSerializer<Any?>, value)

    private fun decodeOk(serializer: KSerializer<*>, text: String, name: String): Any? =
        when (val result = WeaverCodec.decode(serializer, text)) {
            is DecodeResult.Ok -> result.value
            else -> throw AssertionError("$name: $result")
        }

    private fun assertNotOk(where: String, result: DecodeResult<*>) {
        assertFalse("$where must not decode", result is DecodeResult.Ok)
    }

    /** Paths to every key of every object, skipping free-form JSON values (`value`). */
    private fun objectKeyPaths(element: JsonElement, prefix: List<String> = emptyList()): List<List<String>> =
        when (element) {
            is JsonObject -> element.flatMap { (key, child) ->
                val path = prefix + key
                listOf(path) + if (key == "value") emptyList() else objectKeyPaths(child, path)
            }
            is JsonArray -> element.flatMapIndexed { i, child -> objectKeyPaths(child, prefix + "$i") }
            else -> emptyList()
        }

    /** Paths to every object (the root included), skipping free-form JSON values. */
    private fun objectPaths(element: JsonElement, prefix: List<String> = emptyList()): List<List<String>> =
        when (element) {
            is JsonObject -> listOf(prefix) + element.flatMap { (key, child) ->
                if (key == "value") emptyList() else objectPaths(child, prefix + key)
            }
            is JsonArray -> element.flatMapIndexed { i, child -> objectPaths(child, prefix + "$i") }
            else -> emptyList()
        }

    private fun removeAt(element: JsonElement, path: List<String>): JsonElement =
        update(element, path.dropLast(1)) { JsonObject(it.jsonObject - path.last()) }

    private fun addAt(element: JsonElement, path: List<String>, key: String): JsonElement =
        update(element, path) {
            buildJsonObject {
                it.jsonObject.forEach { (k, v) -> put(k, v) }
                put(key, JsonPrimitive(true))
            }
        }

    private fun update(element: JsonElement, path: List<String>, change: (JsonElement) -> JsonElement): JsonElement {
        if (path.isEmpty()) return change(element)
        val head = path.first()
        return when (element) {
            is JsonObject -> JsonObject(element + (head to update(element.getValue(head), path.drop(1), change)))
            is JsonArray -> JsonArray(
                element.mapIndexed { i, child -> if (i == head.toInt()) update(child, path.drop(1), change) else child },
            )
            else -> error("cannot descend into $element at $head")
        }
    }
}
