package io.github.junhcha.weaver.review

import io.github.junhcha.weaver.courier.CourierClient
import io.github.junhcha.weaver.schema.*
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test
import java.io.File

class ProposalValidationTest {
    private fun example() = CourierClient.decoded<Proposal>(
        File(System.getProperty("weaver.schemaDir"), "examples/Proposal.json").readText())
    private fun reject(p: Proposal, message: String) {
        val error = assertThrows(IllegalArgumentException::class.java) { ReviewDraft.validateEdits(p, emptyList()) }
        assertEquals(message, error.message)
    }
    @Test fun reviewCounterexamplesFailBeforeAnyVerdictCanBeQueued() {
        val p = example()
        listOf(
            Verdict.Edit("/schedules/0/end", JsonPrimitive("2026-11-08T14:00:00+09:00")) to "schedule end must follow start",
            Verdict.Edit("/relations/0/to", JsonPrimitive("e999")) to "dangling entity reference",
        ).forEach { (edit, message) ->
            val error = assertThrows(IllegalArgumentException::class.java) {
                ReviewDraft.verdict(p, Verdict.Kind.EDIT, listOf(Verdict.Edit("/entities/0/ask", JsonNull), edit),
                    null, "", emptyMap(), "2026-11-08T00:00:00Z")
            }
            assertEquals(message, error.message)
        }
    }
    @Test fun endMustBeStrictlyAfterStartUsingInstants() {
        val p = example()
        listOf(p.schedules[0].start, "2026-11-08T07:00:00+02:00").forEach { end ->
            reject(p.copy(schedules = p.schedules.map { it.copy(end = end) }), "schedule end must follow start")
        }
        ReviewDraft.validateEdits(p, listOf(Verdict.Edit("/schedules/0/end", JsonPrimitive("2026-11-08T07:00:00Z"))))
    }
    @Test fun entityRefsMustBeUnique() {
        val p = example()
        reject(p.copy(entities = p.entities + p.entities[0]), "proposal entities require unique non-null refs")
    }
    @Test fun refsMustBeUniqueAcrossAllGroups() {
        val p = example()
        listOf(
            p.copy(entryDrafts = p.entryDrafts.map { it.copy(ref = "e1") }),
            p.copy(schedules = p.schedules + p.schedules[0]),
            p.copy(schedules = p.schedules.map { it.copy(ref = "d1") }),
        ).forEach { reject(it, "local refs must be unique across proposal items") }
    }
    @Test fun relationEndpointsMustBeKnownAndThisIsAllowed() {
        val p = example()
        listOf("from", "to").forEach { field ->
            assertThrows(IllegalArgumentException::class.java) {
                ReviewDraft.validateEdits(p, listOf(Verdict.Edit("/relations/0/$field", JsonPrimitive("e999"))))
            }
            ReviewDraft.validateEdits(p, listOf(Verdict.Edit("/relations/0/$field", JsonPrimitive("this"))))
        }
    }
    @Test fun draftAndScheduleEntitiesMustBeKnown() {
        val p = example()
        listOf("entry_drafts", "schedules").forEach { group ->
            listOf("e999", "ent_01ARZ3NDEKTSV4RRFFQ69G5FA9").forEach { ref ->
                assertThrows(IllegalArgumentException::class.java) {
                    ReviewDraft.validateEdits(p, listOf(Verdict.Edit("/$group/0/entities/0", JsonPrimitive(ref))))
                }
            }
        }
    }
    @Test fun changingEntityIdRechecksEveryReference() {
        val p = example()
        val oldId = p.entities[1].entityId!!
        val linked = p.copy(relations = p.relations.map { it.copy(to = oldId) })
        assertThrows(IllegalArgumentException::class.java) {
            ReviewDraft.validateEdits(linked, listOf(Verdict.Edit("/entities/1/entity_id", JsonNull)))
        }
        ReviewDraft.validateEdits(linked, listOf(Verdict.Edit("/entities/1/entity_id", JsonNull),
            Verdict.Edit("/relations/0/to", JsonPrimitive("e2"))))
    }
    @Test fun requestRequesterAndTargetsMustBeKnown() {
        val p = example()
        val d = p.entryDrafts[0]
        listOf(d.request!!.copy(requester = "e999"), d.request.copy(targets = listOf("e999"))).forEach { request ->
            reject(p.copy(entryDrafts = listOf(d.copy(request = request)) + p.entryDrafts.drop(1)), "dangling entity reference")
        }
    }
    @Test fun blockMustReferenceAnotherRequest() {
        val p = example()
        listOf("d2", "d999").forEach { ref ->
            reject(p.copy(entryDrafts = p.entryDrafts.mapIndexed { i, d -> if (i == 1) d.copy(blockedByRequest = ref) else d }),
                "blocked_by_request must reference another request")
        }
        reject(p.copy(entryDrafts = p.entryDrafts.mapIndexed { i, d -> if (i == 0) d.copy(blockedByRequest = "d1") else d }),
            "blocked_by_request must reference another request")
    }
    @Test fun scheduleRequiresExistingScheduleDraftIncludingKindEdits() {
        val p = example()
        reject(p.copy(schedules = p.schedules.map { it.copy(entryRef = "d999") }),
            "schedule entry_ref must reference an 일정 draft")
        assertThrows(IllegalArgumentException::class.java) {
            ReviewDraft.validateEdits(p, listOf(Verdict.Edit("/entry_drafts/1/kind", JsonPrimitive("논의"))))
        }
    }
    @Test fun scheduleMustPreserveDraftBlock() {
        val p = example()
        reject(p.copy(schedules = p.schedules.map { it.copy(blockedByRequest = null) }),
            "schedule must preserve its draft execution block")
    }
    @Test fun strictDecodeStillEnforcesFieldRulesAndRequestKind() {
        val p = example()
        listOf(
            Verdict.Edit("/summary", JsonPrimitive(" ")),
            Verdict.Edit("/entities/0/aliases", JsonArray(listOf(JsonPrimitive("")))),
            Verdict.Edit("/entry_drafts/0/kind", JsonPrimitive("논의")),
            Verdict.Edit("/entry_drafts/1/occurred_at", JsonPrimitive("tomorrow")),
            Verdict.Edit("/schedules/0/start", JsonPrimitive(123)),
        ).forEach { edit -> assertThrows(IllegalArgumentException::class.java) { ReviewDraft.validateEdits(p, listOf(edit)) } }
    }
}
