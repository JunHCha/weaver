package io.github.junhcha.weaver.review

import io.github.junhcha.weaver.courier.*
import io.github.junhcha.weaver.schema.*
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.io.File
import java.io.FileOutputStream
import java.util.UUID

@Serializable
enum class ReviewStatus(val label: String) {
    PENDING("전송 대기"), ERROR("전송 오류 · 재시도 가능"), CONFLICT("충돌 · 기존 파일을 덮어쓰지 않았습니다"),
    STALE("새 revision 도착 또는 문서 없음 · 최신 문서를 다시 확인하세요"), SENT("전송됨(PC 반영 대기)"),
    INFERRED("파일 사라짐 · PC 반영 추정"),
}

@Serializable
data class ReviewRecord(val verdict: Verdict, val tree: String, val status: ReviewStatus,
                        // Never forget a successful publication when a later listing/read fails.
                        val published: Boolean = false)

/** Durable immutable intent plus delivery observations, all inside the backup-excluded exchange dir.
 * Callers hold CourierRuntime.lock. No phone deletion of packages or decisions, even on rejection.
 */
class ReviewStore(private val directory: File) {
    class PackageMissingException : IllegalStateException("문서가 없습니다")

    private val json = Json { encodeDefaults = true }
    init { check(directory.exists() || directory.mkdirs()) }
    private fun file(id: String, revision: Long) = File(directory, "$id.r$revision.$EXTENSION")
    private fun atomic(final: File, text: String) {
        val temp = File(directory, ".${UUID.randomUUID()}.tmp")
        try {
            FileOutputStream(temp).use { it.write(text.toByteArray()); it.fd.sync() }
            check(temp.renameTo(final)) { "local_write_failed" }
        } finally { temp.delete() }
    }
    private fun save(record: ReviewRecord) =
        atomic(file(record.verdict.packageId, record.verdict.revision), json.encodeToString(record))
    private fun watermark(id: String) = File(directory, "$id.latest")
    private fun requireId(id: String) = require(Regex("[A-Za-z0-9_-]+").matches(id)) { "invalid package id" }

    fun records(): List<ReviewRecord> = directory.listFiles().orEmpty().filter { it.extension == EXTENSION }
        .map { json.decodeFromString<ReviewRecord>(it.readText()) }

    fun observeRevision(id: String, revision: Long) {
        requireId(id)
        val known = watermark(id).takeIf { it.exists() }?.readText()?.toLong() ?: 0L
        if (revision > known) atomic(watermark(id), revision.toString())
    }

    /** The newest revision in proposals/, never older than one already seen on this phone. */
    fun latest(folder: CourierFolder, id: String): ReviewPackage {
        requireId(id)
        val candidates = folder.list("proposals").mapNotNull { entry ->
            CourierRules.revision.matchEntire(entry.name)?.takeIf { it.groupValues[1] == id }
                ?.groupValues?.get(2)?.toLongOrNull()?.let { it to entry }
        }
        val newest = candidates.maxByOrNull { it.first } ?: throw PackageMissingException()
        check(newest.second.regular) { "최신 revision이 일반 파일이 아닙니다" }
        check(newest.first >= (watermark(id).takeIf { it.exists() }?.readText()?.toLong() ?: 0L)) {
            "최신 revision이 아직 동기화되지 않았습니다"
        }
        observeRevision(id, newest.first)
        val p = folder.read("proposals", newest.second.name).use {
            CourierClient.decoded<ReviewPackage>(CourierClient.boundedJson(it))
        }
        check(p.packageId == id && p.revision == newest.first) { "id_mismatch" }
        return p
    }

    fun queue(folder: CourierFolder, p: ReviewPackage, verdict: Verdict) {
        check(p.packageId == verdict.packageId && p.revision == verdict.revision)
        require(verdict.answers.size == p.questions.size && verdict.answers.zip(p.questions).all { (a, q) -> a in q.choices })
        check(latest(folder, p.packageId) == p) { "새 revision이 도착했습니다. 최신 문서를 다시 확인하세요" }
        check(WeaverCodec.encode(verdict).toByteArray().size <= CourierRules.maxJson) { "too_large" }
        val existing = file(p.packageId, p.revision)
        if (existing.exists()) {
            val record = json.decodeFromString<ReviewRecord>(existing.readText())
            check(record.verdict == verdict && record.tree == folder.identity) { "이미 제출한 revision의 내용은 바꿀 수 없습니다" }
        } else save(ReviewRecord(verdict, folder.identity, ReviewStatus.PENDING))
    }

    /** Publish queued verdicts and classify delivery; false means a retry is useful. */
    fun synchronize(folder: CourierFolder): Boolean {
        var success = true
        for (record in records().filter { it.tree == folder.identity }) {
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
                    // A recreated directory or changed root proves nothing about PC processing.
                    if (folder.created("decisions")) ReviewStatus.SENT else ReviewStatus.INFERRED
                } else {
                    val latest = latest(folder, v.packageId)
                    if (latest.revision != v.revision) ReviewStatus.STALE else {
                        folder.publish("decisions", name) { it.write(WeaverCodec.encode(v).toByteArray()) }
                        ReviewStatus.SENT
                    }
                }
            } catch (_: SecurityException) {
                save(record.copy(status = ReviewStatus.ERROR))
                throw SecurityException("Courier access lost")
            } catch (_: Exception) { ReviewStatus.ERROR }
            save(record.copy(status = status,
                published = record.published || status in setOf(ReviewStatus.SENT, ReviewStatus.INFERRED)))
            if (status in setOf(ReviewStatus.ERROR, ReviewStatus.CONFLICT, ReviewStatus.STALE)) success = false
        }
        return success
    }

    private companion object { const val EXTENSION = "review" }
}
