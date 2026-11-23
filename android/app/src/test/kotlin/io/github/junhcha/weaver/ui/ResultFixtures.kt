package io.github.junhcha.weaver.ui

import io.github.junhcha.weaver.schema.DocBullet
import io.github.junhcha.weaver.schema.DocSection
import io.github.junhcha.weaver.schema.DocSectionKind
import io.github.junhcha.weaver.schema.DocumentView
import io.github.junhcha.weaver.schema.RefLinkView
import io.github.junhcha.weaver.schema.TreeNodeView
import io.github.junhcha.weaver.transport.ContractFixtures
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject

/** Recorded results fixtures decoded into the Kotlin models, plus small invented documents for ordering tests. */
internal object ResultFixtures {
    private val json = Json { ignoreUnknownKeys = true }
    const val documentId = "5f19b95b9ab4aa0142341ad1"

    val tree: List<TreeNodeView> get() = (ContractFixtures["results.tree"].responseBody!!.jsonObject["items"] as JsonArray)
        .map { json.decodeFromJsonElement(TreeNodeView.serializer(), it) }

    val document: DocumentView get() =
        json.decodeFromJsonElement(DocumentView.serializer(), ContractFixtures["results.document"].responseBody!!)

    private fun bullet(id: String, text: String, day: String? = null) = DocBullet(id, text, listOf("t_$id"), null, day, null)

    /** Every section, deliberately out of reading order. */
    val full: DocumentView get() = document.copy(
        sections = listOf(
            DocSection(DocSectionKind.CAUTIONS, "주의할 이력", listOf(bullet("c1", "견적 단가 재협상 이력", "2026-11-05"))),
            DocSection(DocSectionKind.WAITING, "기다리는 중", listOf(bullet("w1", "김철수 회신 대기"))),
            DocSection(DocSectionKind.REMAINING, "남은 일", document.sections[1].bullets),
            DocSection(DocSectionKind.CURRENT, "현재 상태", document.sections[0].bullets),
        ),
        references = listOf(RefLinkView("회사", "notes/장비 비교.md", "장비 비교", true)),
    )

    /** Long enough that its last bullet starts below the first screen. */
    val long: DocumentView get() = document.copy(
        sections = listOf(DocSection(DocSectionKind.CURRENT, "현재 상태", (1..40).map { bullet("b$it", "항목 $it") })),
    )

    /** `GET /read/references` answer with one note. */
    fun withReference(body: kotlinx.serialization.json.JsonElement): kotlinx.serialization.json.JsonElement =
        JsonObject(body.jsonObject + ("items" to json.parseToJsonElement(
            """[{"sphere":"회사","path":"notes/장비 비교.md","heading":"장비 비교","home":false}]""")))
}
