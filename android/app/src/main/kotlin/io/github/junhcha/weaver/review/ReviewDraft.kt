package io.github.junhcha.weaver.review

import io.github.junhcha.weaver.schema.*
import kotlinx.serialization.json.*

/** Replacement-only RFC 6901: no insertion, implicit keys, '-' or leading-zero indexes. */
object JsonPointer {
    fun tokens(path: String): List<String> {
        require(path.startsWith('/') && !Regex("~(?![01])").containsMatchIn(path)) { "잘못된 JSON Pointer" }
        return path.substring(1).split('/').map { it.replace("~1", "/").replace("~0", "~") }
    }
    fun replace(root: JsonElement, path: String, value: JsonElement): JsonElement {
        val tokens = tokens(path)
        fun walk(node: JsonElement, depth: Int): JsonElement {
            if (depth == tokens.size) return value
            val token = tokens[depth]
            return when (node) {
                is JsonObject -> {
                    require(token in node) { "수정 경로가 없습니다" }
                    JsonObject(node + (token to walk(node.getValue(token), depth + 1)))
                }
                is JsonArray -> {
                    require(Regex("0|[1-9][0-9]*").matches(token)) { "잘못된 배열 인덱스" }
                    val index = token.toIntOrNull()
                    require(index != null && index in node.indices) { "배열 범위를 벗어났습니다" }
                    JsonArray(node.mapIndexed { i, child -> if (i == index) walk(child, depth + 1) else child })
                }
                else -> throw IllegalArgumentException("수정 경로가 없습니다")
            }
        }
        return walk(root, 0)
    }
}

data class ReviewQuestion(val path: String, val ask: Proposal.Ask, val summary: String, val confidence: Double) {
    val confidenceLabel: String get() = "확신도 ${kotlin.math.round(confidence * 100).toInt()}%"
    // Schedule ask edits are excluded by the v1 server whitelist.
    val canConfirm: Boolean get() = ask.choices.isEmpty() && !path.startsWith("/schedules/")
}

/** Keep the whitelist aligned with server/src/weaver/loom/edits.py; never widen v1. */
object ReviewDraft {
    private val fields = mapOf(
        "entities" to setOf("name", "type", "aliases", "entity_id", "ask"),
        "relations" to setOf("from", "to", "type", "reason", "ask"),
        "entry_drafts" to setOf("kind", "field", "value", "occurred_at", "entities", "ask"),
        "schedules" to setOf("title", "start", "end", "entities"),
    )
    private fun label(value: JsonElement): String = (value as? JsonPrimitive)?.content ?: value.toString()
    private fun entityType(type: Proposal.EntityType): String = when (type) {
        Proposal.EntityType.PERSON -> "인물"; Proposal.EntityType.ORG -> "조직"
        Proposal.EntityType.PROJECT -> "프로젝트"; Proposal.EntityType.TOPIC -> "주제"
        Proposal.EntityType.MEETING -> "회의"
    }
    fun questions(p: Proposal): List<ReviewQuestion> = buildList {
        fun name(ref: String): String = if (ref == "this") "이 자료" else
            p.entities.firstOrNull { it.ref == ref || it.entityId == ref }?.name ?: ref
        p.entities.forEachIndexed { i, item -> item.ask?.let {
            add(ReviewQuestion("/entities/$i", it, "${item.name} · ${entityType(item.type)}", item.confidence))
        } }
        p.relations.forEachIndexed { i, item -> item.ask?.let {
            add(ReviewQuestion("/relations/$i", it,
                "${name(item.from)} → ${name(item.to)} · ${label(WeaverCodec.json.encodeToJsonElement(item.type))}", item.confidence))
        } }
        p.entryDrafts.forEachIndexed { i, item -> item.ask?.let {
            add(ReviewQuestion("/entry_drafts/$i", it,
                "${label(WeaverCodec.json.encodeToJsonElement(item.kind))} · ${item.field ?: "필드 없음"} · ${label(item.value)} · ${item.occurredAt}", item.confidence))
        } }
        p.schedules.forEachIndexed { i, item -> item.ask?.let {
            add(ReviewQuestion("/schedules/$i", it, "${item.title} · ${item.start}", item.confidence))
        } }
    }

    fun validateEdits(p: Proposal, edits: List<Verdict.Edit>): Proposal {
        val original = WeaverCodec.json.encodeToJsonElement(p).jsonObject
        var data: JsonElement = original
        edits.forEach { edit ->
            val parts = JsonPointer.tokens(edit.path)
            if (parts != listOf("summary")) {
                require(parts.size >= 3 && parts[2] in (fields[parts[0]] ?: emptySet())) { "수정할 수 없는 필드입니다" }
                require(parts.size == 3 || parts[2] in setOf("aliases", "entities", "value")) { "중첩 수정 불가" }
                // Resolve the index in the original too, matching the server's lineage checks.
                require(Regex("0|[1-9][0-9]*").matches(parts[1])) { "잘못된 배열 인덱스" }
                val index = parts[1].toIntOrNull()
                val group = original.getValue(parts[0]).jsonArray
                require(index != null && index in group.indices) { "배열 범위를 벗어났습니다" }
                val item = group[index].jsonObject
                if (parts[2] == "ask") require(edit.value == JsonNull) { "ask에는 null만 허용됩니다" }
                if (parts[0] == "entities" && parts[2] == "entity_id" && edit.value != JsonNull) {
                    val choices = (item["ask"] as? JsonObject)?.get("choices")?.jsonArray.orEmpty()
                    require(choices.any { it.jsonObject["entity_id"] == edit.value }) { "원래 ask 선택지만 허용됩니다" }
                }
                if (parts[0] == "entry_drafts" && parts[2] == "kind") {
                    require((item["kind"] == JsonPrimitive("요청")) == (edit.value == JsonPrimitive("요청"))) { "요청 종류는 불변입니다" }
                }
            }
            data = JsonPointer.replace(data, edit.path, edit.value)
        }
        return when (val decoded = WeaverCodec.decode<Proposal>(data.toString())) {
            is DecodeResult.Ok -> decoded.value.also { ProposalValidation.validate(it) }
            else -> throw IllegalArgumentException("수정 후 제안이 스키마에 맞지 않습니다")
        }
    }

    /** A choice can link an entity and acknowledge its ask. Free text needs server regeneration. */
    fun verdict(p: Proposal, kind: Verdict.Kind, edits: List<Verdict.Edit>, reason: Verdict.RejectReason?,
                instruction: String, answers: Map<String, String>, reviewedAt: String, confirmations: Set<String> = emptySet()): Verdict {
        val applied = edits.toMutableList()
        val regeneration = mutableListOf<String>()
        if (kind in setOf(Verdict.Kind.APPROVE, Verdict.Kind.EDIT, Verdict.Kind.REVISE)) {
            questions(p).forEach { q ->
                val answer = answers[q.path]?.trim().orEmpty()
                if (answer.isNotEmpty()) {
                    val choice = q.ask.choices.firstOrNull { it.entityId == answer }
                    if (kind != Verdict.Kind.REVISE && q.path.startsWith("/entities/") && choice != null) {
                        applied += Verdict.Edit("${q.path}/entity_id", JsonPrimitive(choice.entityId))
                        applied += Verdict.Edit("${q.path}/ask", JsonNull)
                    } else regeneration += "${q.path}: ${q.ask.question}\n답: $answer"
                } else if (q.path in confirmations && q.canConfirm) {
                    applied += Verdict.Edit("${q.path}/ask", JsonNull)
                }
            }
        }
        val finalKind = when {
            regeneration.isNotEmpty() -> Verdict.Kind.REVISE
            kind == Verdict.Kind.APPROVE && applied.isNotEmpty() -> Verdict.Kind.EDIT
            else -> kind
        }
        if (finalKind in setOf(Verdict.Kind.APPROVE, Verdict.Kind.EDIT)) {
            val corrected = validateEdits(p, applied)
            val unresolved = questions(corrected).filter { !it.path.startsWith("/schedules/") }
            require(unresolved.isEmpty()) {
                "미해결 질문 ${unresolved.size}개: ${unresolved.joinToString("; ") { it.summary }}. 질문에 답하거나 다시 짜기를 선택하세요"
            }
        }
        val instructions = buildList {
            if (instruction.isNotBlank()) add(instruction.trim())
            addAll(regeneration)
            if (finalKind == Verdict.Kind.REVISE && applied.isNotEmpty()) add("원하는 수정값: ${WeaverCodec.json.encodeToString(applied)}")
        }.joinToString("\n\n")
        return Verdict(1, p.proposalId, p.rev, finalKind,
            if (finalKind == Verdict.Kind.EDIT) applied else emptyList(),
            if (finalKind == Verdict.Kind.REJECT) reason else null,
            if (finalKind == Verdict.Kind.REVISE) instructions.takeIf { it.isNotBlank() }
                ?: throw IllegalArgumentException("다시 짤 지시를 입력하세요") else null, reviewedAt)
    }
}
