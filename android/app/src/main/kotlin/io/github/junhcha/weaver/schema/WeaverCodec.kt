package io.github.junhcha.weaver.schema

import kotlinx.serialization.KSerializer
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.serializer

/** Outcome of reading one Courier JSON file. */
sealed interface DecodeResult<out T> {
    data class Ok<T>(val value: T) : DecodeResult<T>

    /**
     * `schema_version` is an integer other than [SCHEMA_VERSION]. The file is not processed;
     * [diagnostic] is meant for the app log or a user-visible error list.
     */
    data class UnsupportedVersion(val model: String, val found: Long) : DecodeResult<Nothing> {
        val diagnostic: String
            get() = "$model schema_version $found is not supported (expected $SCHEMA_VERSION); skipped"
    }

    /** Malformed JSON or a violation of the v1 contract. */
    data class Invalid(val model: String, val reason: String) : DecodeResult<Nothing> {
        val diagnostic: String get() = "$model is invalid: $reason"
    }
}

/**
 * Strict JSON for schema/ v1: unknown keys are rejected, nulls are written explicitly and
 * no key has a default, so every declared key must be present when decoding.
 */
object WeaverCodec {
    val json: Json = Json {
        ignoreUnknownKeys = false
        explicitNulls = true
        encodeDefaults = true
        isLenient = false
        allowSpecialFloatingPointValues = false
    }

    // A JSON integer token only: 1.0, 1e0 or "1" are not schema_version 1.
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
        if (found != SCHEMA_VERSION.toLong()) return DecodeResult.UnsupportedVersion(model, found)
        // Decode from the original text, not the parsed tree, so integer fields keep the
        // lexical integer-token rule (rev 1.0 is rejected by the streaming decoder).
        return try {
            DecodeResult.Ok(json.decodeFromString(serializer, text))
        } catch (e: SerializationException) {
            DecodeResult.Invalid(model, e.message ?: "does not match the v1 model")
        } catch (e: IllegalArgumentException) {
            DecodeResult.Invalid(model, e.message ?: "violates a v1 constraint")
        }
    }

    inline fun <reified T> encode(value: T): String = json.encodeToString(serializer<T>(), value)
}
