package io.github.junhcha.weaver.review

import io.github.junhcha.weaver.courier.*
import io.github.junhcha.weaver.schema.*
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test
import org.junit.Rule
import org.junit.rules.TemporaryFolder
import java.io.File

class ReviewTest {
    @get:Rule val temporary = TemporaryFolder()
    private val id = "prp_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    private val time = "2026-11-07T00:00:00Z"
    private fun proposal(): Proposal {
        val dir = File(System.getProperty("weaver.schemaDir"), "examples")
        val file = dir.walkTopDown().first { it.isFile && it.name.startsWith("Proposal") && it.extension == "json" }
        val p = CourierClient.decoded<Proposal>(file.readText()).copy(proposalId = id)
        return p.copy(entities = p.entities.map { it.copy(ask = null) },
            relations = p.relations.map { it.copy(ask = null) },
            entryDrafts = p.entryDrafts.map { it.copy(ask = null) })
    }
    private class Folder : CourierFolder {
        val files = mutableMapOf<String, String>()
        var fail = false
        var published = 0
        override val identity = "synthetic-root"
        override fun list(folder: String) = files.keys.filter { it.startsWith("$folder/") }
            .map { CourierEntry(it.substringAfter('/'), 0) }
        override fun read(folder: String, name: String) = files.getValue("$folder/$name").byteInputStream()
        override fun publish(folder: String, name: String, write: (java.io.OutputStream) -> Unit) {
            check(!fail) { "synthetic_failure" }
            check("$folder/$name" !in files)
            val out = java.io.ByteArrayOutputStream(); write(out)
            files["$folder/$name"] = out.toString("UTF-8"); published++
        }
        override fun delete(folder: String, name: String): Unit = error("phone must not delete review files")
        fun put(p: Proposal) { files["proposals/${p.proposalId}.r${p.rev}.json"] = WeaverCodec.encode(p) }
    }
    @Test fun missingProposalUsesDedicatedException() {
        val store = ReviewStore(temporary.newFolder())
        assertThrows(ReviewStore.ProposalMissingException::class.java) { store.latest(Folder(), id) }
    }

    @Test fun capturedAtUsesLocalTimeAndPreservesInvalidInput() {
        assertEquals("2026-11-07 09:00", io.github.junhcha.weaver.ui.capturedAtLabel(
            "2026-11-07T00:00:00Z", java.time.ZoneId.of("Asia/Seoul")))
        assertEquals("2026-11-06 19:00", io.github.junhcha.weaver.ui.capturedAtLabel(
            "2026-11-07T00:00:00Z", java.time.ZoneId.of("America/New_York")))
        assertEquals("invalid synthetic timestamp", io.github.junhcha.weaver.ui.capturedAtLabel("invalid synthetic timestamp"))
    }

    @Test fun fourVerdictsRoundTripAndPublishOnce() {
        Verdict.Kind.entries.forEach { kind ->
            val folder = Folder(); val p = proposal(); folder.put(p)
            val store = ReviewStore(temporary.newFolder())
            val v = ReviewDraft.verdict(p, kind, if (kind == Verdict.Kind.EDIT)
                listOf(Verdict.Edit("/summary", JsonPrimitive("Corrected"))) else emptyList(),
                if (kind == Verdict.Kind.REJECT) Verdict.RejectReason.OTHER else null,
                if (kind == Verdict.Kind.REVISE) "Try again" else "", emptyMap(), time)
            assertEquals(emptyList<String>(), io.github.junhcha.weaver.schema.SharedSchema.errors("Verdict", WeaverCodec.encode(v)))
            store.queue(folder, p, v); store.queue(folder, p, v)
            assertTrue(store.synchronize(folder)); assertTrue(store.synchronize(folder))
            assertEquals(1, folder.published)
            assertEquals(v, CourierClient.decoded<Verdict>(folder.files.getValue("decisions/$id.r${p.rev}.json")))
            assertEquals(ReviewStatus.SENT, store.records().single().status)
        }
    }
    @Test fun retryAfterRestartKeepsExactBodyAndRejectsConflicts() {
        val root = temporary.newFolder(); val folder = Folder(); val p = proposal(); folder.put(p)
        val v = Verdict(1, id, p.rev, Verdict.Kind.REJECT, emptyList(), Verdict.RejectReason.OTHER, null, time)
        ReviewStore(root).queue(folder, p, v)
        folder.fail = true
        assertFalse(ReviewStore(root).synchronize(folder))
        assertEquals(ReviewStatus.ERROR, ReviewStore(root).records().single().status)
        folder.fail = false
        assertTrue(ReviewStore(root).synchronize(folder))
        assertEquals(v, ReviewStore(root).records().single().verdict)
        assertThrows(IllegalStateException::class.java) {
            ReviewStore(root).queue(folder, p, v.copy(rejectReason = Verdict.RejectReason.DUPLICATE))
        }
        folder.files["decisions/$id.r${p.rev}.json"] = "different"
        assertFalse(ReviewStore(root).synchronize(folder))
        assertEquals(ReviewStatus.CONFLICT, ReviewStore(root).records().single().status)
        assertEquals("different", folder.files["decisions/$id.r${p.rev}.json"])
    }
    @Test fun newerRevisionBlocksOpenAndQueuedOldRevisionEvenAfterResync() {
        val folder = Folder(); val p = proposal(); folder.put(p)
        val root = temporary.newFolder(); val store = ReviewStore(root)
        val v = Verdict(1, id, p.rev, Verdict.Kind.APPROVE, emptyList(), null, null, time)
        store.queue(folder, p, v)
        folder.put(p.copy(rev = p.rev + 1))
        assertEquals(p.rev + 1, store.latest(folder, id).rev)
        assertFalse(store.synchronize(folder)); assertEquals(0, folder.published)
        assertThrows(IllegalStateException::class.java) { store.queue(folder, p, v) }
        folder.files.remove("proposals/$id.r${p.rev + 1}.json")
        assertThrows(IllegalStateException::class.java) { ReviewStore(root).latest(folder, id) }
    }
    @Test fun disappearanceIsOnlyInferredAndNeverRepublishes() {
        val folder = Folder(); val p = proposal(); folder.put(p)
        val store = ReviewStore(temporary.newFolder())
        store.queue(folder, p, Verdict(1, id, p.rev, Verdict.Kind.REVISE, emptyList(), null, "Again", time))
        assertTrue(store.synchronize(folder))
        folder.files.remove("decisions/$id.r${p.rev}.json")
        assertTrue(store.synchronize(folder))
        assertEquals(ReviewStatus.INFERRED, store.records().single().status)
        assertEquals(1, folder.published)
    }
    @Test fun pointersValidateIndexesEscapesAndImmutableFields() {
        val root = Json.parseToJsonElement("""{"a/b":[{"~key":"before"}]}""")
        assertEquals(JsonPrimitive("after"), JsonPointer.replace(root, "/a~1b/0/~0key", JsonPrimitive("after"))
            .jsonObject["a/b"]!!.jsonArray[0].jsonObject["~key"])
        listOf("/a~2b", "/a~1b/01", "/a~1b/-", "/a~1b/1", "/missing").forEach {
            assertThrows(IllegalArgumentException::class.java) { JsonPointer.replace(root, it, JsonNull) }
        }
        val p = proposal()
        val nested = p.copy(entryDrafts = p.entryDrafts.mapIndexed { i, entry ->
            if (i == 0) entry.copy(value = root) else entry
        })
        val replaced = ReviewDraft.validateEdits(nested,
            listOf(Verdict.Edit("/entry_drafts/0/value/a~1b/0/~0key", JsonPrimitive("after"))))
        assertEquals(JsonPrimitive("after"), replaced.entryDrafts[0].value.jsonObject["a/b"]!!
            .jsonArray[0].jsonObject["~key"])
        assertThrows(IllegalArgumentException::class.java) { ReviewDraft.validateEdits(nested,
            listOf(Verdict.Edit("/entry_drafts/0/value/a~1b/01/~0key", JsonPrimitive("after")))) }
        assertThrows(IllegalArgumentException::class.java) { ReviewDraft.validateEdits(p,
            listOf(Verdict.Edit("/proposal_id", JsonPrimitive(id)))) }
    }
    @Test fun askChoiceUsesAllowedEditWhileFreeTextAndScheduleUseRevise() {
        val base = proposal()
        val choice = Proposal.Choice("ent_01ARZ3NDEKTSV4RRFFQ69G5FAV", "Synthetic choice")
        val ask = Proposal.Ask("Who?", listOf(choice))
        val p = base.copy(entities = base.entities.mapIndexed { i, e -> if (i == 0) e.copy(ask = ask) else e })
        val path = "/entities/0"
        val selected = ReviewDraft.verdict(p, Verdict.Kind.APPROVE, emptyList(), null, "",
            mapOf(path to choice.entityId), time)
        assertEquals(Verdict.Kind.EDIT, selected.verdict)
        assertEquals(listOf("$path/entity_id", "$path/ask"), selected.edits.map { it.path })
        assertNull(ReviewDraft.validateEdits(p, selected.edits).entities[0].ask)
        val free = ReviewDraft.verdict(p, Verdict.Kind.EDIT, emptyList(), null, "",
            mapOf(path to "Somebody else"), time)
        assertEquals(Verdict.Kind.REVISE, free.verdict)
        assertTrue(free.instruction!!.contains("Somebody else"))
        val schedule = base.copy(schedules = base.schedules.map { it.copy(ask = ask) })
        val answer = ReviewDraft.verdict(schedule, Verdict.Kind.APPROVE, emptyList(), null, "",
            mapOf("/schedules/0" to choice.entityId), time)
        assertEquals(Verdict.Kind.REVISE, answer.verdict)
        assertThrows(IllegalArgumentException::class.java) {
            ReviewDraft.verdict(p, Verdict.Kind.APPROVE, emptyList(), null, "", emptyMap(), time)
        }
        assertThrows(IllegalArgumentException::class.java) {
            ReviewDraft.validateEdits(schedule, listOf(Verdict.Edit("/schedules/0/ask", JsonNull)))
        }
    }
    @Test fun rejectAndReviseNeverOfferCalendarCandidate() {
        val p = proposal()
        for (kind in listOf(Verdict.Kind.REJECT, Verdict.Kind.REVISE)) {
            val v = ReviewDraft.verdict(p, kind, emptyList(), Verdict.RejectReason.OTHER, "Try again", emptyMap(), time)
            assertNull(ReviewRuntime.calendarReviewCandidate(p, v))
        }
    }
    @Test fun sameExistingFinalIsSuccessAndDifferentFinalIsNeverReplaced() {
        val folder = Folder(); val p = proposal(); folder.put(p)
        val store = ReviewStore(temporary.newFolder())
        val v = Verdict(1, id, p.rev, Verdict.Kind.APPROVE, emptyList(), null, null, time)
        store.queue(folder, p, v)
        folder.files["decisions/$id.r${p.rev}.json"] = WeaverCodec.encode(v)
        assertTrue(store.synchronize(folder)); assertEquals(0, folder.published)
        folder.files.remove("proposals/$id.r${p.rev}.json")
        assertTrue(store.synchronize(folder))
        assertEquals(ReviewStatus.INFERRED, store.records().single().status)
    }

    @Test fun confirmationCreatesNullEditWithoutRegenerationAndFreeAnswerWins() {
        val base = proposal()
        val ask = Proposal.Ask("Synthetic confirmation", emptyList())
        val p = base.copy(entryDrafts = base.entryDrafts.mapIndexed { i, item ->
            if (i == 0) item.copy(ask = ask) else item
        })
        val path = "/entry_drafts/0"
        val confirmed = ReviewDraft.verdict(p, Verdict.Kind.APPROVE, emptyList(), null, "", emptyMap(), time, setOf(path))
        assertEquals(Verdict.Kind.EDIT, confirmed.verdict)
        assertEquals(listOf(Verdict.Edit("$path/ask", JsonNull)), confirmed.edits)
        assertNull(confirmed.instruction)
        assertNull(ReviewDraft.validateEdits(p, confirmed.edits).entryDrafts[0].ask)
        val error = assertThrows(IllegalArgumentException::class.java) {
            ReviewDraft.verdict(p, Verdict.Kind.APPROVE, emptyList(), null, "", emptyMap(), time)
        }
        assertTrue(error.message!!.contains("미해결 질문 1개"))
        assertTrue(error.message!!.contains(ReviewDraft.questions(p).single().summary))
        val answered = ReviewDraft.verdict(p, Verdict.Kind.APPROVE, emptyList(), null, "",
            mapOf(path to "Synthetic correction"), time, setOf(path))
        assertEquals(Verdict.Kind.REVISE, answered.verdict)
        assertTrue(answered.instruction!!.contains("Synthetic correction"))
        assertTrue(answered.edits.isEmpty())
    }

    @Test fun questionSummariesResolveNamesAndIncludeEveryGroupAndConfidence() {
        val base = proposal()
        val ask = Proposal.Ask("Synthetic confirmation", emptyList())
        val p = base.copy(entities = base.entities.map { it.copy(ask = ask) },
            relations = base.relations.map { it.copy(ask = ask) },
            entryDrafts = base.entryDrafts.map { it.copy(ask = ask) },
            schedules = base.schedules.map { it.copy(ask = ask) })
        val questions = ReviewDraft.questions(p).associateBy { it.path }
        assertTrue(questions.getValue("/entities/0").summary.contains("${p.entities[0].name} · 인물"))
        assertEquals("${p.entities[0].name} → ${p.entities[1].name} · 소속", questions.getValue("/relations/0").summary)
        val entry = p.entryDrafts[0]
        val entrySummary = questions.getValue("/entry_drafts/0").summary
        assertTrue(entrySummary.contains("요청"))
        assertTrue(entrySummary.contains(entry.value.jsonPrimitive.content))
        assertTrue(entrySummary.contains(entry.occurredAt))
        assertEquals("${p.schedules[0].title} · ${p.schedules[0].start}", questions.getValue("/schedules/0").summary)
        assertEquals("확신도 92%", questions.getValue("/entities/0").confidenceLabel)
        assertFalse(questions.getValue("/schedules/0").canConfirm)
        val direct = p.copy(relations = p.relations.map { it.copy(to = p.entities[1].entityId!!) })
        assertEquals(questions.getValue("/relations/0").summary,
            ReviewDraft.questions(direct).first { it.path == "/relations/0" }.summary)
    }

    @Test fun confirmsOnlySupportedEmptyChoiceQuestionsAndListsRemainingTargets() {
        val base = proposal()
        val ask = Proposal.Ask("Synthetic confirmation", emptyList())
        val p = base.copy(entities = base.entities.map { it.copy(ask = ask) },
            relations = base.relations.map { it.copy(ask = ask) })
        val questions = ReviewDraft.questions(p)
        val error = assertThrows(IllegalArgumentException::class.java) {
            ReviewDraft.verdict(p, Verdict.Kind.APPROVE, emptyList(), null, "", emptyMap(), time,
                setOf("/entities/0"))
        }
        assertTrue(error.message!!.contains("미해결 질문 ${questions.size - 1}개"))
        questions.filter { it.path != "/entities/0" }.forEach { assertTrue(error.message!!.contains(it.summary)) }
        val result = ReviewDraft.verdict(p, Verdict.Kind.APPROVE, emptyList(), null, "", emptyMap(), time,
            questions.map { it.path }.toSet())
        assertEquals(Verdict.Kind.EDIT, result.verdict)
        assertEquals(questions.map { Verdict.Edit("${it.path}/ask", JsonNull) }, result.edits)
        val choice = Proposal.Choice("ent_01ARZ3NDEKTSV4RRFFQ69G5FAV", "Synthetic choice")
        val withChoice = base.copy(entities = base.entities.mapIndexed { i, e ->
            if (i == 0) e.copy(ask = Proposal.Ask("Who?", listOf(choice))) else e
        })
        assertThrows(IllegalArgumentException::class.java) {
            ReviewDraft.verdict(withChoice, Verdict.Kind.APPROVE, emptyList(), null, "", emptyMap(), time,
                setOf("/entities/0"))
        }
    }

    @Test fun mixedSubmissionPreservesConfirmedTargetsInReviseInstruction() {
        val base = proposal()
        val ask = Proposal.Ask("Synthetic confirmation", emptyList())
        val p = base.copy(entities = base.entities.map { it.copy(ask = ask) })
        val confirmed = "/entities/0"
        val answered = "/entities/1"
        val answers = mapOf(answered to "Synthetic correction")
        val result = ReviewDraft.verdict(p, Verdict.Kind.APPROVE, emptyList(), null, "", answers, time, setOf(confirmed))
        assertEquals(Verdict.Kind.REVISE, result.verdict)
        assertTrue(result.edits.isEmpty())
        val instruction = result.instruction!!
        assertTrue(instruction.contains("다음 항목은 사용자가 맞다고 확인했으니 내용을 유지하고 다시 묻지 말 것"))
        assertTrue(instruction.contains(ReviewDraft.questions(p).first().summary))
        assertTrue(instruction.contains("Synthetic correction"))
        assertFalse(instruction.contains(confirmed))
        assertFalse(instruction.contains("null"))
        assertTrue(ReviewDraft.hasMixedAnswers(p, answers, setOf(confirmed)))
        assertFalse(ReviewDraft.hasMixedAnswers(p, answers, setOf(answered)))
        assertFalse(ReviewDraft.hasMixedAnswers(p, emptyMap(), setOf(confirmed)))
        assertEquals(emptyList<String>(), SharedSchema.errors("Verdict", WeaverCodec.encode(result)))
    }

    @Test fun summariesBoundEveryGroupAtCodePointBoundaryAndErrorsShowOnlyThree() {
        val base = proposal()
        val ask = Proposal.Ask("Synthetic confirmation", emptyList())
        val longValue = "😀".repeat(100)
        val p = base.copy(entities = base.entities.map { it.copy(name = longValue, ask = ask) },
            relations = base.relations.map { it.copy(ask = ask) },
            entryDrafts = base.entryDrafts.map { it.copy(value = JsonPrimitive(longValue), ask = ask) },
            schedules = base.schedules.map { it.copy(title = longValue, ask = ask) })
        ReviewDraft.questions(p).forEach { q ->
            assertEquals(60, q.summary.codePointCount(0, q.summary.length))
            assertTrue(q.summary.endsWith("…"))
            assertFalse(q.summary.dropLast(1).last().isHighSurrogate())
            assertFalse(q.summary.contains(longValue))
        }
        val many = base.copy(entities = (0..4).map { i -> base.entities[0].copy(ref = "e${i + 1}", name = "Synthetic $i", ask = ask) },
            relations = emptyList(), entryDrafts = emptyList(), schedules = emptyList())
        val questions = ReviewDraft.questions(many)
        val error = assertThrows(IllegalArgumentException::class.java) {
            ReviewDraft.verdict(many, Verdict.Kind.APPROVE, emptyList(), null, "", emptyMap(), time)
        }
        assertTrue(error.message!!.contains("미해결 질문 5개"))
        questions.take(3).forEach { assertTrue(error.message!!.contains(it.summary)) }
        questions.drop(3).forEach { assertFalse(error.message!!.contains(it.summary)) }
    }

}
