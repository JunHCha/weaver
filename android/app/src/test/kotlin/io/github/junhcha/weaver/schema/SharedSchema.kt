package io.github.junhcha.weaver.schema

import com.networknt.schema.InputFormat
import com.networknt.schema.Schema
import com.networknt.schema.SchemaRegistry
import com.networknt.schema.SchemaRegistryConfig
import com.networknt.schema.SpecificationVersion
import java.io.File
import kotlinx.serialization.KSerializer
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject

/**
 * Access to the repository-root schema/ v1 files. Gradle passes the directory as
 * `weaver.schemaDir`, so these tests always read the files the server exported.
 */
object SharedSchema {
    val dir: File = File(
        requireNotNull(System.getProperty("weaver.schemaDir")) { "weaver.schemaDir is not set" },
    )

    private val registry = SchemaRegistry.withDefaultDialect(SpecificationVersion.DRAFT_2020_12) {
        it.schemaRegistryConfig(SchemaRegistryConfig.builder().formatAssertionsEnabled(true).build())
    }
    private val validators = mutableMapOf<String, Schema>()

    /** The five shared wire models of issue #28 and their Kotlin serializers. */
    val models: Map<String, KSerializer<*>> = linkedMapOf(
        "Manifest" to Manifest.serializer(),
        "Receipt" to Receipt.serializer(),
        "Proposal" to Proposal.serializer(),
        "Verdict" to Verdict.serializer(),
        "Alert" to Alert.serializer(),
    )

    fun schemaText(model: String): String = File(dir, "$model.schema.json").readText()

    fun schemaJson(model: String): JsonObject = Json.parseToJsonElement(schemaText(model)).jsonObject

    /** JSON Schema Draft 2020-12 errors (format assertions on); empty means valid. */
    fun errors(model: String, instance: String): List<String> {
        val schema = validators.getOrPut(model) { registry.getSchema(schemaText(model), InputFormat.JSON) }
        return schema.validate(instance, InputFormat.JSON).map { it.toString() }
    }

    /** Every schema/examples/<Model>.json and <Model>-<variant>.json for [model]. */
    fun examples(model: String): List<File> =
        File(dir, "examples").listFiles { f -> f.name == "$model.json" || f.name.startsWith("$model-") }!!
            .sortedBy { it.name }
            .also { check(it.isNotEmpty()) { "no examples for $model" } }
}
