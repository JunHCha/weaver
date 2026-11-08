package io.github.junhcha.weaver.review

import io.github.junhcha.weaver.schema.Proposal
import java.time.OffsetDateTime

/** Full Proposal semantic checks from server/schema/models.py, after strict wire decoding. */
internal object ProposalValidation {
    fun validate(p: Proposal) {
        val entityRefs = p.entities.map { it.ref }
        require(entityRefs.distinct().size == entityRefs.size) { "proposal entities require unique non-null refs" }
        val known = entityRefs.toSet() + p.entities.mapNotNull { it.entityId }
        val allRefs = entityRefs + p.entryDrafts.map { it.ref } + p.schedules.map { it.ref }
        require(allRefs.distinct().size == allRefs.size) { "local refs must be unique across proposal items" }
        val drafts = p.entryDrafts.associateBy { it.ref }
        val requests = p.entryDrafts.filter { it.kind == Proposal.EntryKind.REQUEST }.map { it.ref }.toSet()
        fun entities(refs: List<String>) {
            require(refs.all { it in known }) { "dangling entity reference" }
        }
        p.relations.forEach { entities(listOf(it.from, it.to).filter { ref -> ref != "this" }) }
        p.entryDrafts.forEach { draft ->
            entities(draft.entities)
            draft.request?.let { entities(listOf(it.requester) + it.targets) }
            draft.blockedByRequest?.let {
                require(it in requests && draft.ref !in requests) { "blocked_by_request must reference another request" }
            }
        }
        p.schedules.forEach { schedule ->
            schedule.end?.let {
                require(OffsetDateTime.parse(it).toInstant() > OffsetDateTime.parse(schedule.start).toInstant()) {
                    "schedule end must follow start"
                }
            }
            entities(schedule.entities)
            val draft = drafts[schedule.entryRef]
            require(draft != null && draft.kind == Proposal.EntryKind.SCHEDULE) {
                "schedule entry_ref must reference an 일정 draft"
            }
            require(schedule.blockedByRequest == draft.blockedByRequest) {
                "schedule must preserve its draft execution block"
            }
        }
    }
}
