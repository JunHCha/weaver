package io.github.junhcha.weaver.schema

import java.time.OffsetDateTime
import java.time.format.DateTimeParseException

/**
 * Lexical rules shared by schema/ v1 ($defs Timestamp, *Id, Ref, Digest, PayloadRef,
 * NonEmpty, Confidence). Kotlin models check them in `init`, so a decoded or newly built
 * object already satisfies the same constraints the JSON Schema validates.
 */
internal object Rules {
    private const val ULID = "[0-7][0-9A-HJKMNP-TV-Z]{25}"
    private val timestamp =
        Regex("""\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})""")
    private val ref = Regex("[a-z][1-9][0-9]*")
    private val digest = Regex("[0-9a-f]{64}")
    private val payloadRef = Regex("[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*(?:\\.[A-Za-z0-9_-]+)?")
    private val nonEmpty = Regex("\\S")
    private val entityId = Regex("ent_$ULID")

    fun prefixedId(prefix: String, value: String, field: String) {
        require(Regex("${prefix}_$ULID").matches(value)) {
            "$field must be $prefix + uppercase ULID"
        }
    }

    fun timestamp(value: String, field: String) {
        require(timestamp.matches(value) && parses(value)) {
            "$field must be an RFC 3339 timestamp with explicit offset"
        }
    }

    fun ref(value: String, field: String) {
        require(ref.matches(value)) { "$field must be a local ref like e1" }
    }

    fun entityRef(value: String, field: String) {
        require(ref.matches(value) || entityId.matches(value)) {
            "$field must be a local ref or ent_ ID"
        }
    }

    fun digest(value: String, field: String) {
        require(digest.matches(value)) { "$field must be 64 lowercase hex characters" }
    }

    fun payloadRef(value: String, field: String) {
        require(payloadRef.matches(value)) { "$field must be a safe relative payload key" }
    }

    fun nonEmpty(value: String, field: String) {
        require(nonEmpty.containsMatchIn(value)) { "$field must contain a non-space character" }
    }

    fun confidence(value: Double) {
        require(value.isFinite() && value in 0.0..1.0) { "confidence must be within [0, 1]" }
    }

    fun schemaVersion(value: Int) {
        require(value == SCHEMA_VERSION) { "schema_version must be $SCHEMA_VERSION" }
    }

    private fun parses(value: String): Boolean = try {
        OffsetDateTime.parse(value)
        true
    } catch (_: DateTimeParseException) {
        false
    }
}

/** The only wire version this app reads or writes. */
const val SCHEMA_VERSION: Int = 1
