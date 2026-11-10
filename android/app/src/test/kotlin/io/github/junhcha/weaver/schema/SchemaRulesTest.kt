package io.github.junhcha.weaver.schema

import java.io.File
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Value rules that ModelShapeTest (keys, nullability, enums, types) does not cover.
 *
 * 1. Every constrained `$defs` entry must equal the constants in [SchemaRules].
 * 2. All other rule keywords (oneOf/allOf/if/then/else, inline const/minItems, ...) are pinned
 *    in test resources schema-rules/<Model>.json. When the schema changes, update the Kotlin
 *    `init` checks, then refresh the snapshot with
 *    `./gradlew testDebugUnitTest -Dweaver.updateRuleSnapshots=true` (see build.gradle.kts).
 */
class SchemaRulesTest {
    private fun ints(vararg pairs: Pair<String, Any>): JsonObject = buildJsonObject {
        pairs.forEach { (k, v) ->
            put(k, if (v is Number) JsonPrimitive(v) else if (v is JsonElement) v else JsonPrimitive(v.toString()))
        }
    }

    private fun id(prefix: String) = ints(
        "pattern" to SchemaRules.idPattern(prefix),
        "minLength" to SchemaRules.ID_LENGTH,
        "maxLength" to SchemaRules.ID_LENGTH,
    )

    private val noLineBreak = ints("pattern" to SchemaRules.LINE_BREAK)

    /** Kotlin's view of each constrained `$defs` entry, keyed by definition name. */
    private val expected: Map<String, JsonObject> = mapOf(
        "CaptureId" to id("cap"),
        "SourceId" to id("src"),
        "AlertId" to id("alt"),
        "Timestamp" to ints(
            "pattern" to SchemaRules.TIMESTAMP,
            "format" to SchemaRules.DATE_TIME_FORMAT,
            "not" to noLineBreak,
        ),
        "PayloadRef" to ints("pattern" to SchemaRules.PAYLOAD_REF, "not" to noLineBreak),
        "Digest" to ints(
            "pattern" to SchemaRules.DIGEST,
            "minLength" to SchemaRules.DIGEST_LENGTH,
            "maxLength" to SchemaRules.DIGEST_LENGTH,
        ),
        "NonEmpty" to ints("pattern" to SchemaRules.NON_EMPTY),
        "Revision" to ints("minimum" to SchemaRules.REVISION_MIN),
        "Offset" to ints("minimum" to SchemaRules.OFFSET_MIN),
    )

    @Test
    fun constrainedDefinitionsMatchKotlinConstants() {
        SharedSchema.models.keys.forEach { model ->
            val defs = SharedSchema.schemaJson(model)["\$defs"]?.jsonObject ?: return@forEach
            defs.forEach { (name, def) ->
                val rules = JsonObject(def.jsonObject.filterKeys { it in SCALAR_RULES })
                if (rules.isEmpty()) return@forEach
                val mine = expected[name]
                assertTrue("$model \$defs/$name has rules $rules but no Kotlin counterpart", mine != null)
                assertEquals("$model \$defs/$name", mine, rules)
            }
        }
    }

    @Test
    fun ruleKeywordsMatchSnapshot() {
        val dir = File(requireNotNull(System.getProperty("weaver.ruleSnapshotDir")))
        val update = System.getProperty("weaver.updateRuleSnapshots") == "true"
        SharedSchema.models.keys.forEach { model ->
            val actual = JsonObject(rules(SharedSchema.schemaJson(model)).toSortedMap())
            val file = File(dir, "$model.json")
            if (update) file.writeText(pretty.encodeToString(JsonObject.serializer(), actual) + "\n")
            assertTrue("missing snapshot ${file.name}; run with -Dweaver.updateRuleSnapshots=true", file.isFile)
            assertEquals(
                "$model rule keywords changed: update the Kotlin checks, then the snapshot",
                Json.parseToJsonElement(file.readText()),
                actual,
            )
        }
    }

    /** JSON-pointer-like path → rule keyword value, for every rule keyword in [node]. */
    private fun rules(node: JsonElement, path: String = ""): Map<String, JsonElement> = when (node) {
        is JsonObject -> node.flatMap { (key, child) ->
            val here = "$path/$key"
            when {
                key in RULE_KEYS -> listOf(here to child)
                key == "description" || key == "title" -> emptyList()
                else -> rules(child, here).toList()
            }
        }.toMap()
        is JsonArray -> node.withIndex().flatMap { (i, child) -> rules(child, "$path/$i").toList() }.toMap()
        else -> emptyMap()
    }

    private companion object {
        val SCALAR_RULES = setOf("pattern", "not", "minLength", "maxLength", "minimum", "maximum", "format")
        val RULE_KEYS = SCALAR_RULES + setOf(
            "const", "enum", "minItems", "maxItems", "required", "additionalProperties",
            "oneOf", "allOf", "anyOf", "if", "then", "else", "type",
        )
        val pretty = Json { prettyPrint = true }
    }
}
