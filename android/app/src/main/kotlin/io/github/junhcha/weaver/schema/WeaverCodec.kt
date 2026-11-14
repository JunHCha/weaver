package io.github.junhcha.weaver.schema

import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.KSerializer
import kotlinx.serialization.SerializationException
import kotlinx.serialization.descriptors.PrimitiveKind
import kotlinx.serialization.descriptors.SerialDescriptor
import kotlinx.serialization.descriptors.SerialKind
import kotlinx.serialization.descriptors.StructureKind
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.serializer

/** Outcome of reading one Courier JSON file. */
sealed interface DecodeResult<out T> {
    data class Ok<T>(val value: T) : DecodeResult<T>

    /**
     * `schema_version` is an integer other than the one the model carries. The file is not
     * processed; [diagnostic] is meant for the app log or a user-visible error list.
     */
    data class UnsupportedVersion(val model: String, val found: Long, val expected: Long = SCHEMA_VERSION.toLong()) : DecodeResult<Nothing> {
        val diagnostic: String
            get() = "$model schema_version $found is not supported (expected $expected); skipped"
    }

    /** Malformed JSON or a violation of the contract. */
    data class Invalid(val model: String, val reason: String) : DecodeResult<Nothing> {
        val diagnostic: String get() = "$model is invalid: $reason"
    }
}

/**
 * Strict JSON for schema/: unknown keys are rejected, nulls are written explicitly and
 * every declared key must be present when decoding. Capture files carry `schema_version`
 * [SCHEMA_VERSION]; review, action and alert files carry [REVIEW_SCHEMA_VERSION].
 */
object WeaverCodec {
    val json: Json = Json {
        ignoreUnknownKeys = false
        explicitNulls = true
        encodeDefaults = true
        isLenient = false
        allowSpecialFloatingPointValues = false
    }

    private val REVIEW_MODELS = setOf("Action", "ActionResult", "ReviewPackage", "Verdict", "Alert", "PipelineItem")

    // A JSON integer token only: 1.0, 1e0 or "1" are not a schema_version.
    private val integerToken = Regex("-?(?:0|[1-9][0-9]*)")

    inline fun <reified T> decode(text: String): DecodeResult<T> = decode(serializer<T>(), text)

    fun <T> decode(serializer: KSerializer<T>, text: String): DecodeResult<T> {
        val model = serializer.descriptor.serialName.substringAfterLast('.')
        val root = try {
            json.parseToJsonElement(text)
        } catch (e: SerializationException) {
            return DecodeResult.Invalid(model, "malformed JSON: ${e.message}")
        }
        if (root !is JsonObject) return DecodeResult.Invalid(model, "top level must be an object")
        val version = root["schema_version"]
            ?: return DecodeResult.Invalid(model, "schema_version is missing")
        if (version !is JsonPrimitive || version.isString || !integerToken.matches(version.content)) {
            return DecodeResult.Invalid(model, "schema_version must be a JSON integer token")
        }
        val found = version.content.toLongOrNull()
            ?: return DecodeResult.Invalid(model, "schema_version is out of range")
        val expected = (if (model in REVIEW_MODELS) REVIEW_SCHEMA_VERSION else SCHEMA_VERSION).toLong()
        if (found != expected) return DecodeResult.UnsupportedVersion(model, found, expected)
        // kotlinx accepts quoted numbers ("1") and exponent integers (1e0); the schema and
        // its README do not. Check JSON token types against the model before decoding.
        tokenViolation("", root, serializer.descriptor)?.let { return DecodeResult.Invalid(model, it) }
        return try {
            DecodeResult.Ok(json.decodeFromString(serializer, text))
        } catch (e: SerializationException) {
            DecodeResult.Invalid(model, e.message ?: "does not match the model")
        } catch (e: IllegalArgumentException) {
            DecodeResult.Invalid(model, e.message ?: "violates a constraint")
        }
    }

    inline fun <reified T> encode(value: T): String = json.encodeToString(serializer<T>(), value)

    private val numberToken = Regex("-?(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")

    /**
     * Walks [element] along [descriptor] and returns the first JSON token whose type differs
     * from the model: integer fields need integer tokens, number fields unquoted numbers and
     * string/enum fields JSON strings. Structural mismatches are left to the decoder.
     */
    @OptIn(ExperimentalSerializationApi::class)
    private fun tokenViolation(path: String, element: JsonElement, descriptor: SerialDescriptor): String? {
        if (element is JsonNull || descriptor.serialName.startsWith("kotlinx.serialization.json.")) return null
        return when (descriptor.kind) {
            StructureKind.CLASS -> (element as? JsonObject)?.let { obj ->
                (0 until descriptor.elementsCount).firstNotNullOfOrNull { i ->
                    val name = descriptor.getElementName(i)
                    obj[name]?.let { tokenViolation("$path/$name", it, descriptor.getElementDescriptor(i)) }
                }
            }
            StructureKind.LIST -> (element as? JsonArray)?.let { array ->
                array.withIndex().firstNotNullOfOrNull { (i, child) ->
                    tokenViolation("$path/$i", child, descriptor.getElementDescriptor(0))
                }
            }
            PrimitiveKind.BYTE, PrimitiveKind.SHORT, PrimitiveKind.INT, PrimitiveKind.LONG ->
                if (element.isLiteral(integerToken)) null else "$path must be a JSON integer token"
            PrimitiveKind.FLOAT, PrimitiveKind.DOUBLE ->
                if (element.isLiteral(numberToken)) null else "$path must be a JSON number"
            PrimitiveKind.STRING, SerialKind.ENUM ->
                if (element is JsonPrimitive && element.isString) null else "$path must be a JSON string"
            else -> null
        }
    }

    private fun JsonElement.isLiteral(token: Regex): Boolean =
        this is JsonPrimitive && !isString && token.matches(content)
}
