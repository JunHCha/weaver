package io.github.junhcha.weaver.schema

import java.io.File
import kotlinx.serialization.KSerializer
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * Contract between the Kotlin models and the repository-root schema/ files.
 * The JSON Schema validator is the referee: Kotlin must accept what the schema accepts in
 * the shared examples, reject the invalid fixtures the schema rejects, and every JSON the
 * Kotlin models write must validate against the same schema.
 */
class SchemaContractTest {
    @Test
    fun captureHostsAcceptKnownValuesAndRejectInvalidValues() {
        val manifest = Json.parseToJsonElement(SharedSchema.examples("Manifest").first().readText()).jsonObject
        for (host in listOf("phone", "mac", "device")) {
            val text = JsonObject(manifest + ("capture_host" to JsonPrimitive(host))).toString()
            assertTrue(SharedSchema.errors("Manifest", text).isEmpty())
            assertTrue(WeaverCodec.decode<Manifest>(text) is DecodeResult.Ok)
        }
        val bad = JsonObject(manifest + ("capture_host" to JsonPrimitive("unknown"))).toString()
        assertFalse(SharedSchema.errors("Manifest", bad).isEmpty())
        assertTrue(WeaverCodec.decode<Manifest>(bad) is DecodeResult.Invalid)
        val item = Json.parseToJsonElement(SharedSchema.examples("PipelineItem").first().readText()).jsonObject
        val hosts = JsonObject(item + ("capture_hosts" to JsonArray(listOf(JsonPrimitive("mac"), JsonPrimitive("unknown"))))).toString()
        assertTrue(SharedSchema.errors("PipelineItem", hosts).isEmpty())
        assertTrue(WeaverCodec.decode<PipelineItem>(hosts) is DecodeResult.Ok)
    }

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
                requiredKeyPaths(example, SharedSchema.schemaJson(model)).forEach { path ->
                    val mutated = removeAt(example, path).toString()
                    val where = "${file.name} without ${path.joinToString("/")}"
                    assertFalse("$where must fail the schema", SharedSchema.errors(model, mutated).isEmpty())
                    assertNotOk(where, WeaverCodec.decode(serializer, mutated))
                }
            }
        }
    }

    /** Defaults and map entries are optional, including inside nested wire objects. */
    private fun requiredKeyPaths(value: JsonElement, root: JsonObject): List<List<String>> {
        val defs = root["\$defs"]?.jsonObject.orEmpty()
        fun resolve(input: JsonObject): JsonObject {
            input["\$ref"]?.jsonPrimitive?.content?.let { return resolve(defs.getValue(it.substringAfterLast('/')).jsonObject) }
            input["anyOf"]?.jsonArray?.let { branches ->
                return resolve(branches.first { it.jsonObject["type"]?.jsonPrimitive?.content != "null" }.jsonObject)
            }
            return input
        }
        fun visit(node: JsonElement, schema: JsonObject, path: List<String>): List<List<String>> {
            val shape = resolve(schema)
            return when (node) {
                is JsonObject -> {
                    val required = shape["required"]?.jsonArray?.map { it.jsonPrimitive.content }?.toSet().orEmpty()
                    val properties = shape["properties"]?.jsonObject.orEmpty()
                    node.flatMap { (key, child) ->
                        val at = path + key
                        val nested = properties[key] as? JsonObject ?: shape["additionalProperties"] as? JsonObject
                        (if (key in required) listOf(at) else emptyList()) +
                            (nested?.let { visit(child, it, at) } ?: emptyList())
                    }
                }
                is JsonArray -> (shape["items"] as? JsonObject)?.let { item -> node.withIndex().flatMap { (index, child) -> visit(child, item, path + index.toString()) } }.orEmpty()
                else -> emptyList()
            }
        }
        return visit(value, root, emptyList())
    }

    @Test
    fun unknownVersionsAreSkippedWithDiagnostic() {
        SharedSchema.models.forEach { (model, serializer) ->
            val example = Json.parseToJsonElement(SharedSchema.examples(model).first().readText()).jsonObject
            val expected = SharedSchema.version(model)
            listOf(0L, 99L, 3 - expected).forEach { version ->
                val text = JsonObject(example + ("schema_version" to JsonPrimitive(version))).toString()
                assertFalse("$model v$version must fail the schema", SharedSchema.errors(model, text).isEmpty())
                val result = WeaverCodec.decode(serializer, text)
                assertEquals("$model v$version", DecodeResult.UnsupportedVersion(model, version, expected), result)
                assertTrue((result as DecodeResult.UnsupportedVersion).diagnostic.contains("schema_version $version"))
            }
        }
    }

    @Test
    fun integerFieldsRequireIntegerTokens() {
        // JSON Schema treats 1.0 and 1e0 as integers; the README makes this lexical rule the
        // consumer's job, so only the Kotlin side is asserted for those tokens.
        listOf(
            Triple("ReviewPackage", "\"schema_version\": 2", "\"schema_version\": 2.0"),
            Triple("ReviewPackage", "\"schema_version\": 2", "\"schema_version\": 2e0"),
            Triple("ReviewPackage", "\"revision\": 1", "\"revision\": 1.0"),
            Triple("Verdict", "\"revision\": 1", "\"revision\": 1e0"),
            Triple("Verdict", "\"revision\": 1", "\"revision\": 1E+0"),
            Triple("ApprovedSchedule", "\"revision\": 1", "\"revision\": 1.0"),
            Triple("Manifest", "\"size\": 45", "\"size\": 45.0"),
            Triple("Manifest", "\"size\": 45", "\"size\": 4.5e1"),
        ).forEach { (model, from, to) -> assertKotlinInvalid(model, from, to) }
    }

    @Test
    fun quotedNumbersFailSchemaAndKotlin() {
        listOf(
            Triple("ReviewPackage", "\"schema_version\": 2", "\"schema_version\": \"2\""),
            Triple("ReviewPackage", "\"revision\": 1", "\"revision\": \"1\""),
            Triple("Verdict", "\"revision\": 1", "\"revision\": \"1\""),
            Triple("Manifest", "\"size\": 45", "\"size\": \"45\""),
        ).forEach { (model, from, to) ->
            val text = assertKotlinInvalid(model, from, to)
            assertFalse("$model $to must fail the schema", SharedSchema.errors(model, text).isEmpty())
        }
    }

    @Test
    fun largeRevisionsAreAccepted() {
        // revision is unbounded in the schema; Kotlin keeps it as Long.
        val verdict = SharedSchema.examples("Verdict").first().readText()
            .replaceFirst("\"revision\": 1", "\"revision\": 2147483648")
        assertEquals(emptyList<String>(), SharedSchema.errors("Verdict", verdict))
        val result = WeaverCodec.decode<Verdict>(verdict)
        assertEquals(2_147_483_648L, (result as DecodeResult.Ok).value.revision)
    }

    @Test
    fun appIsStricterThanSchemaForUnrealisticTimestamps() {
        // Documented in Primitives.kt: java.time rejects offsets beyond ±18:00 and more than
        // nine fraction digits, which no producer writes. Pinned so the gap stays known.
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
        val text = alert.replace("\"alert_id\": \"alt_01ARZ3NDEKTSV4RRFFQ69G5FA0\"", "\"alert_id\": \"alt_01ARZ3NDEKTSV4RRFFQ69G5FA0\\n\"")
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

        val base = Verdict(REVIEW_SCHEMA_VERSION, "pkg_0123456789abcdef", 2, Verdict.Action.APPROVE, listOf("가"), null, null)
        listOf(
            base,
            base.copy(action = Verdict.Action.EDIT, editedBody = "수정"),
            base.copy(feedback = "다시 정리"),
            base.copy(action = Verdict.Action.REJECT),
        ).forEach {
            assertEquals(it.action.name, emptyList<String>(), SharedSchema.errors("Verdict", WeaverCodec.encode(it)))
        }
    }

    @Test
    fun kotlinRefusesToBuildMismatchedVerdictBranches() {
        val approve = Verdict(REVIEW_SCHEMA_VERSION, "pkg_0123456789abcdef", 1, Verdict.Action.APPROVE, emptyList(), null, null)
        listOf(
            { approve.copy(action = Verdict.Action.REJECT, feedback = "x") },
            { approve.copy(editedBody = "x") },
            { approve.copy(action = Verdict.Action.EDIT) },
            { approve.copy(revision = 0) },
            { approve.copy(schemaVersion = 1) },
            { approve.copy(packageId = " ") },
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

    /** Keys whose values are free-form JSON or a map with data-defined keys. */
    private val FREE_FORM = setOf("value", "stage_times")

    /** Paths to every key of every object, skipping the [FREE_FORM] values. */
    private fun objectKeyPaths(element: JsonElement, prefix: List<String> = emptyList()): List<List<String>> =
        when (element) {
            is JsonObject -> element.flatMap { (key, child) ->
                val path = prefix + key
                listOf(path) + if (key in FREE_FORM) emptyList() else objectKeyPaths(child, path)
            }
            is JsonArray -> element.flatMapIndexed { i, child -> objectKeyPaths(child, prefix + "$i") }
            else -> emptyList()
        }

    /** Paths to every object (the root included), skipping the [FREE_FORM] values. */
    private fun objectPaths(element: JsonElement, prefix: List<String> = emptyList()): List<List<String>> =
        when (element) {
            is JsonObject -> listOf(prefix) + element.flatMap { (key, child) ->
                if (key in FREE_FORM) emptyList() else objectPaths(child, prefix + key)
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
