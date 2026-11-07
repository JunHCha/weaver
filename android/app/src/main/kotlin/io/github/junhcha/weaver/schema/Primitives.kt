package io.github.junhcha.weaver.schema

import java.time.OffsetDateTime
import java.time.format.DateTimeParseException

/**
 * Value constraints of schema/ v1 `$defs`, copied verbatim. RuleConstantsTest compares every
 * constant with the exported schema, so a schema change that is not mirrored here fails.
 */
internal object SchemaRules {
    const val TIMESTAMP = """^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"""
    const val REF = "^[a-z][1-9][0-9]*$"
    const val DIGEST = "^[0-9a-f]{64}$"
    const val DIGEST_LENGTH = 64
    const val PAYLOAD_REF = "^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*(?:\\.[A-Za-z0-9_-]+)?$"
    const val NON_EMPTY = "\\S"
    const val POINTER = "^(?:/(?:[^~/]|~[01])*)*$"

    /** `not: {pattern}` on Timestamp, Ref and PayloadRef. */
    const val LINE_BREAK = "[\\r\\n]"

    const val ID_LENGTH = 30
    const val CONFIDENCE_MIN = 0
    const val CONFIDENCE_MAX = 1
    const val REVISION_MIN = 1
    const val OFFSET_MIN = 0
    const val DATE_TIME_FORMAT = "date-time"

    /** Prefixes of ID `$defs`: CaptureId=cap, SourceId=src, ProposalId=prp, AlertId=alt, EntityId=ent, EntryId=evt. */
    fun idPattern(prefix: String): String = "^${prefix}_[0-7][0-9A-HJKMNP-TV-Z]{25}$"
}

/**
 * Checks the [SchemaRules] in model `init`, so a decoded or newly built object already
 * satisfies the constraints the JSON Schema validates.
 *
 * The app is deliberately stricter than the schema in two places, both values a v1 producer
 * never writes: timestamps must also parse with java.time (offsets within ±18:00, at most
 * nine fraction digits), and integers must fit in a Kotlin Long.
 */
internal object Rules {
    private val timestamp = Regex(SchemaRules.TIMESTAMP)
    private val ref = Regex(SchemaRules.REF)
    private val digest = Regex(SchemaRules.DIGEST)
    private val payloadRef = Regex(SchemaRules.PAYLOAD_REF)
    private val nonEmpty = Regex(SchemaRules.NON_EMPTY)
    private val lineBreak = Regex(SchemaRules.LINE_BREAK)
    private val ids = mutableMapOf<String, Regex>()
    private val entityId = id("ent")

    // ECMA-262 `$` (JSON Schema) only matches at the very end; java.util.regex `$` also
    // matches before a final line break. Line breaks are therefore rejected explicitly.
    private fun Regex.schemaMatches(value: String): Boolean =
        !lineBreak.containsMatchIn(value) && containsMatchIn(value)

    private fun id(prefix: String): Regex =
        synchronized(ids) { ids.getOrPut(prefix) { Regex(SchemaRules.idPattern(prefix)) } }

    fun prefixedId(prefix: String, value: String, field: String) {
        require(id(prefix).schemaMatches(value)) { "$field must be $prefix + uppercase ULID" }
    }

    fun timestamp(value: String, field: String) {
        require(timestamp.schemaMatches(value) && parses(value)) {
            "$field must be an RFC 3339 timestamp with explicit offset"
        }
    }

    fun ref(value: String, field: String) {
        require(ref.schemaMatches(value)) { "$field must be a local ref like e1" }
    }

    fun entityRef(value: String, field: String) {
        require(ref.schemaMatches(value) || entityId.schemaMatches(value)) {
            "$field must be a local ref or ent_ ID"
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

    fun confidence(value: Double) {
        require(value.isFinite() && value >= SchemaRules.CONFIDENCE_MIN && value <= SchemaRules.CONFIDENCE_MAX) {
            "confidence must be within [${SchemaRules.CONFIDENCE_MIN}, ${SchemaRules.CONFIDENCE_MAX}]"
        }
    }

    fun revision(value: Long) {
        require(value >= SchemaRules.REVISION_MIN) { "rev must be >= ${SchemaRules.REVISION_MIN}" }
    }

    fun offset(value: Long, field: String) {
        require(value >= SchemaRules.OFFSET_MIN) { "$field must be nonnegative" }
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
