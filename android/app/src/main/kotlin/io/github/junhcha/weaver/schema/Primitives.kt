package io.github.junhcha.weaver.schema

import java.time.OffsetDateTime
import java.time.format.DateTimeParseException

/**
 * Value constraints of the schema/ `$defs`, copied verbatim. RuleConstantsTest compares every
 * constant with the exported schema, so a schema change that is not mirrored here fails.
 */
internal object SchemaRules {
    const val TIMESTAMP = """^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"""
    const val DIGEST = "^[0-9a-f]{64}$"
    const val DIGEST_LENGTH = 64
    const val PAYLOAD_REF = "^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*(?:\\.[A-Za-z0-9_-]+)?$"
    const val NON_EMPTY = "\\S"

    /** `not: {pattern}` on Timestamp and PayloadRef. */
    const val LINE_BREAK = "[\\r\\n]"

    const val ID_LENGTH = 30
    const val ACTION_ID = "^act_[0-9a-f]{32}$"
    const val ACTION_ID_LENGTH = 36
    const val REVISION_MIN = 1
    const val OFFSET_MIN = 0
    const val DATE_TIME_FORMAT = "date-time"

    /** Prefixes of ID `$defs`: CaptureId=cap, SourceId=src, AlertId=alt. */
    fun idPattern(prefix: String): String = "^${prefix}_[0-7][0-9A-HJKMNP-TV-Z]{25}$"
}

/**
 * Checks the [SchemaRules] in model `init`, so a decoded or newly built object already
 * satisfies the constraints the JSON Schema validates.
 *
 * The app is deliberately stricter than the schema in two places, both values a producer
 * never writes: timestamps must also parse with java.time (offsets within ±18:00, at most
 * nine fraction digits), and integers must fit in a Kotlin Long.
 */
internal object Rules {
    private val timestamp = Regex(SchemaRules.TIMESTAMP)
    private val digest = Regex(SchemaRules.DIGEST)
    private val payloadRef = Regex(SchemaRules.PAYLOAD_REF)
    private val nonEmpty = Regex(SchemaRules.NON_EMPTY)
    private val lineBreak = Regex(SchemaRules.LINE_BREAK)
    private val actionId = Regex(SchemaRules.ACTION_ID)
    private val ids = mutableMapOf<String, Regex>()

    // ECMA-262 `$` (JSON Schema) only matches at the very end; java.util.regex `$` also
    // matches before a final line break. Line breaks are therefore rejected explicitly.
    private fun Regex.schemaMatches(value: String): Boolean =
        !lineBreak.containsMatchIn(value) && containsMatchIn(value)

    private fun id(prefix: String): Regex =
        synchronized(ids) { ids.getOrPut(prefix) { Regex(SchemaRules.idPattern(prefix)) } }

    fun prefixedId(prefix: String, value: String, field: String) {
        require(id(prefix).schemaMatches(value)) { "$field must be $prefix + uppercase ULID" }
    }

    fun actionId(value: String) {
        require(actionId.schemaMatches(value)) { "action_id must be act_ + 32 lowercase hex characters" }
    }

    fun timestamp(value: String, field: String) {
        require(timestamp.schemaMatches(value) && parses(value)) {
            "$field must be an RFC 3339 timestamp with explicit offset"
        }
    }

    fun digest(value: String, field: String) {
        require(digest.schemaMatches(value)) { "$field must be 64 lowercase hex characters" }
    }

    fun payloadRef(value: String, field: String) {
        require(payloadRef.schemaMatches(value)) { "$field must be a safe relative payload key" }
    }

    fun nonEmpty(value: String, field: String) {
        require(nonEmpty.containsMatchIn(value)) { "$field must contain a non-space character" }
    }

    fun revision(value: Long) {
        require(value >= SchemaRules.REVISION_MIN) { "revision must be >= ${SchemaRules.REVISION_MIN}" }
    }

    fun offset(value: Long, field: String) {
        require(value >= SchemaRules.OFFSET_MIN) { "$field must be nonnegative" }
    }

    fun schemaVersion(value: Int, expected: Int = SCHEMA_VERSION) {
        require(value == expected) { "schema_version must be $expected" }
    }

    private fun parses(value: String): Boolean = try {
        OffsetDateTime.parse(value)
        true
    } catch (_: DateTimeParseException) {
        false
    }
}

/** Wire version of capture files (Manifest, Receipt). */
const val SCHEMA_VERSION: Int = 1

/** Wire version of review, action and alert files. */
const val REVIEW_SCHEMA_VERSION: Int = 2
