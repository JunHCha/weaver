package io.github.junhcha.weaver.courier

import io.github.junhcha.weaver.schema.*
import io.github.junhcha.weaver.schema.v2.ReviewPackage
import io.github.junhcha.weaver.schema.v2.ApprovedSchedule
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.io.File
import java.io.InputStream
import java.nio.charset.CodingErrorAction
import java.nio.ByteBuffer
import java.security.MessageDigest

/** Runs on an IO worker under CourierRuntime.lock. Every file fails independently. */
class CourierClient(
    private val folder: CourierFolder,
    private val db: CourierDatabase,
    private val cleanupEnabled: Boolean = false,
    private val now: () -> Long = System::currentTimeMillis,
) {
    /**
     * [unreadable] lists final-looking names in proposals/ that were present but rejected. Consumers must treat
     * them as "still there, state unknown" rather than deleted, so a partial or invalid file never clears state.
     */
    data class Inbox(
        val receipts: List<Receipt>, val proposals: List<Proposal>, val alerts: List<Alert>,
        val unreadable: Set<String> = emptySet(), val packages: List<ReviewPackage> = emptyList(),
        val schedules: List<ApprovedSchedule> = emptyList(),
    )

    fun scan(): Inbox {
        val receipts = mutableListOf<Receipt>()
        val proposals = mutableListOf<Proposal>()
        val alerts = mutableListOf<Alert>()
        val packages = mutableListOf<ReviewPackage>()
        val unreadable = mutableSetOf<String>()
        val schedules = mutableListOf<ApprovedSchedule>()
        (CourierRules.folders + "proposals/schedules").forEach { directory ->
            folder.list(directory).forEach { entry ->
                val name = entry.name
                if (directory == "proposals" && name == "schedules" && !entry.regular) return@forEach
                if (CourierRules.conflict(name)) {
                    db.diagnostic(directory, name, "sync_conflict")
                    return@forEach
                }
                if (CourierRules.ignored(name)) {
                    if (CourierRules.phoneTemporary(directory, name) && entry.modifiedAt > 0 &&
                        now() - entry.modifiedAt >= CourierRules.temporaryAge) {
                        attempt(directory, name) { folder.delete(directory, name) }
                    }
                    return@forEach
                }
                // Only PC-owned JSON is consumed; outbox payloads are never interpreted as JSON.
                if (directory !in listOf("receipts", "proposals", "proposals/schedules")) return@forEach
                val read = attempt(directory, name) {
                    check(entry.regular && CourierRules.safeName(name)) { "not_regular_file" }
                    val text = folder.read(directory, name).use(::boundedJson)
                    when {
                        directory == "proposals/schedules" -> {
                            val schedule = decoded<ApprovedSchedule>(text)
                            check(name == "${schedule.threadId}.json") { "id_mismatch" }
                            schedules.add(schedule)
                        }
                        directory == "receipts" && CourierRules.capture.matches(name) -> {
                            val receipt = decoded<Receipt>(text)
                            check(name == "${receipt.captureId}.json") { "id_mismatch" }
                            receipts.add(receipt)
                        }
                        directory == "proposals" && CourierRules.revision.matches(name) -> {
                            val version = WeaverCodec.json.parseToJsonElement(text).jsonObject["schema_version"]?.jsonPrimitive
                            if (version != null && !version.isString && version.content == "2") {
                                val pkg = decoded<ReviewPackage>(text)
                                check(name == "${pkg.packageId}.r${pkg.revision}.json") { "id_mismatch" }
                                packages.add(pkg)
                            } else {
                                val proposal = decoded<Proposal>(text)
                                check(name == "${proposal.proposalId}.r${proposal.rev}.json") { "id_mismatch" }
                                proposals.add(proposal)
                            }
                        }
                        directory == "proposals" && CourierRules.alert.matches(name) -> {
                            val alert = decoded<Alert>(text)
                            check(name == "alert_${alert.alertId}.json") { "id_mismatch" }
                            alerts.add(alert)
                        }
                        else -> error("invalid_filename")
                    }
                }
                if (!read && directory == "proposals") unreadable.add(name)
            }
        }
        return Inbox(receipts, proposals.groupBy { it.proposalId }.values.map { versions ->
            versions.maxBy { it.rev }
        }, alerts, unreadable, packages.groupBy { it.packageId }.values.map { it.maxBy { p -> p.revision } }, schedules)
    }

    /** Returns false if a publication failed, allowing WorkManager backoff. */
    fun synchronize(inbox: Inbox = scan()): Boolean {
        var successful = true
        db.captures().forEach { capture ->
            val manifest = capture.manifest
            val id = manifest.captureId
            val receipt = inbox.receipts.firstOrNull { it.captureId == id }
            if (receipt != null && receipt.sha256 != manifest.sha256) {
                db.diagnostic("receipts", "$id.json", "hash_mismatch")
            }
            try {
                if (receipt?.sha256 == manifest.sha256 && capture.state in listOf("PENDING", "SENT")) {
                    db.state(id, "RECEIVED", now())
                } else if (capture.state in listOf("PENDING", "SENT")) {
                    publishCapture(capture)
                    if (capture.state == "PENDING") db.state(id, "SENT", now())
                }
                if (cleanupEnabled) cleanup(capture, receipt?.sha256 == manifest.sha256)
            } catch (error: SecurityException) { throw error
            } catch (error: Exception) {
                db.diagnostic("outbox", "$id.json", errorCode(error))
                successful = false
            }
        }
        return successful
    }

    private fun publishCapture(capture: CourierDatabase.Capture) {
        val m = capture.manifest
        check(CourierRules.safeName(m.payload) && m.payload.startsWith("${m.captureId}.") && m.payload != "${m.captureId}.json") { "unsafe_payload" }
        // A restart can see an already-published body/manifest: verify exact bytes, never overwrite.
        val existing = folder.list("outbox").map { it.name }.toSet()
        if (m.payload in existing) {
            folder.read("outbox", m.payload).use {
                val result = digest(it)
                check(result.first == m.sha256 && result.second == m.size) { "existing_payload_mismatch" }
            }
        } else {
            folder.publish("outbox", m.payload) { output ->
                File(capture.stagedPath).inputStream().use { input ->
                    val hash = MessageDigest.getInstance("SHA-256")
                    var size = 0L
                    val buffer = ByteArray(64 * 1024)
                    while (true) {
                        val count = input.read(buffer)
                        if (count < 0) break
                        output.write(buffer, 0, count); hash.update(buffer, 0, count); size += count
                    }
                    check(hash.digest().hex() == m.sha256 && size == m.size) { "staged_payload_changed" }
                }
            }
        }
        val bytes = WeaverCodec.encode(m).toByteArray()
        check(bytes.size <= CourierRules.maxJson) { "too_large" }
        if ("${m.captureId}.json" in existing) {
            check(folder.read("outbox", "${m.captureId}.json").use {
                readBounded(it).contentEquals(bytes)
            }) { "existing_manifest_mismatch" }
        } else folder.publish("outbox", "${m.captureId}.json") { it.write(bytes) }
    }

    /** Local minimum waits are conservative delays, not Syncthing remote acknowledgements. OFF by default. */
    private fun cleanup(c: CourierDatabase.Capture, receiptMatches: Boolean) {
        val id = c.manifest.captureId
        if (c.state == "DONE") {
            if (receiptMatches) folder.delete("receipts", "$id.json")
            return
        }
        if (now() - c.changedAt < CourierRules.cleanupWait) return
        when (c.state) {
            "RECEIVED" -> if (receiptMatches) {
                folder.delete("outbox", c.manifest.payload)
                db.state(id, "BODY_DELETED", now())
            }
            "BODY_DELETED" -> {
                folder.delete("outbox", "$id.json")
                db.state(id, "MANIFEST_DELETED", now())
            }
            "MANIFEST_DELETED" -> {
                if (receiptMatches) folder.delete("receipts", "$id.json")
                db.state(id, "DONE", now())
            }
        }
    }

    fun publishVerdict(verdict: Verdict) {
        val name = "${verdict.proposalId}.r${verdict.rev}.json"
        val bytes = WeaverCodec.encode(verdict).toByteArray()
        check(bytes.size <= CourierRules.maxJson) { "too_large" }
        if (folder.list("decisions").any { it.name == name }) {
            check(folder.read("decisions", name).use {
                readBounded(it).contentEquals(bytes)
            }) { "existing_verdict_mismatch" }
        } else folder.publish("decisions", name) { it.write(bytes) }
    }

    private fun attempt(directory: String, name: String, block: () -> Unit): Boolean {
        try { block(); return true
        } catch (error: SecurityException) { throw error
        } catch (error: Exception) { db.diagnostic(directory, name, errorCode(error)); return false }
    }

    companion object {
        fun readBounded(input: InputStream): ByteArray {
            val output = java.io.ByteArrayOutputStream()
            val buffer = ByteArray(8192)
            while (output.size() <= CourierRules.maxJson) {
                val count = input.read(buffer, 0, minOf(buffer.size, CourierRules.maxJson + 1 - output.size()))
                if (count < 0) break
                output.write(buffer, 0, count)
            }
            return output.toByteArray()
        }
        fun boundedJson(input: InputStream): String {
            val bytes = readBounded(input)
            check(bytes.size <= CourierRules.maxJson) { "too_large" }
            return Charsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(bytes)).toString()
        }
        inline fun <reified T> decoded(text: String): T = when (val result = WeaverCodec.decode<T>(text)) {
            is DecodeResult.Ok -> result.value
            is DecodeResult.UnsupportedVersion -> error("unknown_version")
            is DecodeResult.Invalid -> error("invalid_schema")
        }
        fun digest(input: InputStream): Pair<String, Long> {
            val digest = MessageDigest.getInstance("SHA-256")
            var size = 0L
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                digest.update(buffer, 0, count); size += count
            }
            return digest.digest().hex() to size
        }
        fun errorCode(error: Exception): String = when (error) {
            is IllegalStateException -> error.message?.takeIf { it.matches(Regex("[a-z_]+")) } ?: "io_failed"
            else -> error.javaClass.simpleName
        }
    }
}

internal fun ByteArray.hex(): String = joinToString("") { "%02x".format(it.toInt() and 255) }
