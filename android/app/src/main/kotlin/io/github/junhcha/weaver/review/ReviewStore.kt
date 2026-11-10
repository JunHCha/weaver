package io.github.junhcha.weaver.review

import io.github.junhcha.weaver.courier.*
import io.github.junhcha.weaver.schema.*
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.io.File
import java.io.FileOutputStream
import java.util.UUID
import io.github.junhcha.weaver.schema.v2.ReviewPackage
import io.github.junhcha.weaver.schema.v2.Verdict as VerdictV2

@Serializable
enum class ReviewStatus(val label: String) {
    PENDING("전송 대기"), ERROR("전송 오류 · 재시도 가능"), CONFLICT("충돌 · 기존 파일을 덮어쓰지 않았습니다"),
    STALE("새 rev 도착 또는 제안 없음 · 최신 제안을 다시 확인하세요"), SENT("전송됨(PC 반영 대기)"),
    INFERRED("파일 사라짐 · PC 반영 추정"),
}

@Serializable
data class ReviewRecord(
    val verdict: Verdict, val tree: String, val status: ReviewStatus,
    // Never forget a successful publication when a later listing/read fails.
    val published: Boolean = false,
    val proposal: Proposal? = null,
)

@Serializable
data class ReviewRecordV2(val verdict: VerdictV2, val tree: String, val status: ReviewStatus,
                          val published: Boolean = false)

/** Durable immutable intent plus delivery observations, all inside the backup-excluded exchange dir.
 * Callers hold CourierRuntime.lock. No phone deletion of proposals or decisions, even on rejection.
 */
class ReviewStore(private val directory: File) {
    class ProposalMissingException : IllegalStateException("제안이 없습니다")

    private val json = Json { encodeDefaults = true }
    init { check(directory.exists() || directory.mkdirs()) }
    private fun name(v: Verdict) = "${v.proposalId}.r${v.rev}.json"
    fun records(): List<ReviewRecord> = directory.listFiles().orEmpty().filter { it.extension == "json" }
        .map { json.decodeFromString<ReviewRecord>(it.readText()) }.sortedByDescending { it.verdict.reviewedAt }
    private fun save(r: ReviewRecord) = atomic(File(directory, name(r.verdict)), json.encodeToString(r))
    private fun atomic(final: File, text: String) {
        val temp = File(directory, ".${UUID.randomUUID()}.tmp")
        try {
            FileOutputStream(temp).use { it.write(text.toByteArray()); it.fd.sync() }
            check(temp.renameTo(final)) { "local_write_failed" }
        } finally { temp.delete() }
    }
    fun observeRevision(id: String, rev: Long) {
        require(Regex("prp_${CourierRules.ulid}").matches(id))
        val watermark = File(directory, "$id.latest")
        val known = watermark.takeIf { it.exists() }?.readText()?.toLong() ?: 0L
        if (rev > known) atomic(watermark, rev.toString())
    }
    fun latest(folder: CourierFolder, id: String): Proposal {
        require(Regex("prp_${CourierRules.ulid}").matches(id))
        val candidates = folder.list("proposals").mapNotNull { entry ->
            CourierRules.revision.matchEntire(entry.name)?.takeIf { it.groupValues[1] == id }
                ?.groupValues?.get(2)?.toLongOrNull()?.let { it to entry }
        }
        val newest = candidates.maxByOrNull { it.first } ?: throw ProposalMissingException()
        check(newest.second.regular) { "최신 rev가 일반 파일이 아닙니다" }
        val watermark = File(directory, "$id.latest")
        check(newest.first >= (watermark.takeIf { it.exists() }?.readText()?.toLong() ?: 0L)) { "최신 rev가 아직 동기화되지 않았습니다" }
        observeRevision(id, newest.first)
        val p = folder.read("proposals", newest.second.name).use { CourierClient.decoded<Proposal>(CourierClient.boundedJson(it)) }
        check(p.proposalId == id && p.rev == newest.first) { "id_mismatch" }
        return p
    }
    fun queue(folder: CourierFolder, p: Proposal, verdict: Verdict) {
        check(p.proposalId == verdict.proposalId && p.rev == verdict.rev)
        check(latest(folder, p.proposalId).rev == p.rev) { "새 rev가 도착했습니다. 최신 제안을 다시 확인하세요" }
        check(WeaverCodec.encode(verdict).toByteArray().size <= CourierRules.maxJson) { "too_large" }
        val existing = records().firstOrNull { name(it.verdict) == name(verdict) }
        if (existing != null) {
            check(existing.verdict == verdict && existing.tree == folder.identity) { "이미 제출한 rev의 내용은 바꿀 수 없습니다" }
            return
        }
        save(ReviewRecord(verdict, folder.identity, ReviewStatus.PENDING, proposal = p))
    }
    fun latestPackage(folder: CourierFolder, id: String): ReviewPackage {
        require(Regex("prp_${CourierRules.ulid}").matches(id))
        val candidates = folder.list("proposals").mapNotNull { entry ->
            CourierRules.revision.matchEntire(entry.name)?.takeIf { it.groupValues[1] == id }
                ?.groupValues?.get(2)?.toLongOrNull()?.let { it to entry }
        }
        val newest = candidates.maxByOrNull { it.first } ?: throw ProposalMissingException()
        check(newest.second.regular)
        val watermark = File(directory, "$id.latest")
        check(newest.first >= (watermark.takeIf { it.exists() }?.readText()?.toLong() ?: 0L))
        observeRevision(id, newest.first)
        val p = folder.read("proposals", newest.second.name).use { CourierClient.decoded<ReviewPackage>(CourierClient.boundedJson(it)) }
        check(p.packageId == id && p.revision == newest.first) { "id_mismatch" }
        return p
    }
    private fun v2File(id: String, rev: Long) = File(directory, "$id.r$rev.v2")
    fun recordsV2(): List<ReviewRecordV2> = directory.listFiles().orEmpty().filter { it.extension == "v2" }
        .map { json.decodeFromString<ReviewRecordV2>(it.readText()) }
    fun queue(folder: CourierFolder, p: ReviewPackage, verdict: VerdictV2) {
        check(p.packageId == verdict.packageId && p.revision == verdict.revision)
        check(latestPackage(folder, p.packageId) == p) { "새 rev가 도착했습니다. 최신 제안을 다시 확인하세요" }
        check(WeaverCodec.encode(verdict).toByteArray().size <= CourierRules.maxJson) { "too_large" }
        val file = v2File(p.packageId, p.revision)
        if (file.exists()) {
            val existing = json.decodeFromString<ReviewRecordV2>(file.readText())
            check(existing.verdict == verdict && existing.tree == folder.identity)
        } else atomic(file, json.encodeToString(ReviewRecordV2(verdict, folder.identity, ReviewStatus.PENDING)))
    }
    private fun synchronizeV2(folder: CourierFolder): Boolean {
        var success = true
        for (record in recordsV2().filter { it.tree == folder.identity }) {
            if (record.status in setOf(ReviewStatus.STALE, ReviewStatus.CONFLICT, ReviewStatus.INFERRED)) continue
            val v = record.verdict
            val name = "${v.packageId}.r${v.revision}.json"
            val status = try {
                val decision = folder.list("decisions").firstOrNull { it.name == name }
                if (decision != null) {
                    val same = decision.regular && folder.read("decisions", name).use {
                        CourierClient.readBounded(it).contentEquals(WeaverCodec.encode(v).toByteArray())
                    }
                    if (!same) ReviewStatus.CONFLICT
                    else if (record.published && !folder.created("proposals") && folder.list("proposals").none { it.name == name }) ReviewStatus.INFERRED
                    else ReviewStatus.SENT
                } else if (record.published) {
                    if (folder.created("decisions")) ReviewStatus.SENT else ReviewStatus.INFERRED
                } else {
                    val latest = latestPackage(folder, v.packageId)
                    if (latest.revision != v.revision) ReviewStatus.STALE else {
                        folder.publish("decisions", name) { it.write(WeaverCodec.encode(v).toByteArray()) }
                        ReviewStatus.SENT
                    }
                }
            } catch (_: SecurityException) {
                atomic(v2File(v.packageId, v.revision), json.encodeToString(record.copy(status = ReviewStatus.ERROR)))
                throw SecurityException("Courier access lost")
            } catch (_: Exception) { ReviewStatus.ERROR }
            atomic(v2File(v.packageId, v.revision), json.encodeToString(record.copy(status = status,
                published = record.published || status in setOf(ReviewStatus.SENT, ReviewStatus.INFERRED))))
            if (status in setOf(ReviewStatus.ERROR, ReviewStatus.CONFLICT, ReviewStatus.STALE)) success = false
        }
        return success
    }
    fun synchronize(folder: CourierFolder): Boolean {
        var success = true
        for (record in records().filter { it.tree == folder.identity }) {
            if (record.status in setOf(ReviewStatus.STALE, ReviewStatus.CONFLICT, ReviewStatus.INFERRED)) continue
            val v = record.verdict
            val status = try {
                val decision = folder.list("decisions").firstOrNull { it.name == name(v) }
                if (decision != null) {
                    val same = decision.regular && folder.read("decisions", name(v)).use {
                        CourierClient.readBounded(it).contentEquals(WeaverCodec.encode(v).toByteArray())
                    }
                    if (!same) ReviewStatus.CONFLICT
                    else if (record.published && !folder.created("proposals") &&
                        folder.list("proposals").none { it.name == name(v) }) ReviewStatus.INFERRED
                    else ReviewStatus.SENT
                } else if (record.published) {
                    // A recreated directory or changed root proves nothing about PC processing.
                    if (folder.created("decisions")) ReviewStatus.SENT else ReviewStatus.INFERRED
                } else {
                    val latest = latest(folder, v.proposalId)
                    if (latest.rev != v.rev) ReviewStatus.STALE else {
                        folder.publish("decisions", name(v)) { it.write(WeaverCodec.encode(v).toByteArray()) }
                        ReviewStatus.SENT
                    }
                }
            } catch (_: SecurityException) { save(record.copy(status = ReviewStatus.ERROR)); throw SecurityException("Courier access lost")
            } catch (_: Exception) { ReviewStatus.ERROR }
            save(record.copy(status = status,
                published = record.published || status in setOf(ReviewStatus.SENT, ReviewStatus.INFERRED)))
            if (status in setOf(ReviewStatus.ERROR, ReviewStatus.CONFLICT, ReviewStatus.STALE)) success = false
        }
        return synchronizeV2(folder) && success
    }
}
