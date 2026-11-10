package io.github.junhcha.weaver.review

import android.content.Context
import io.github.junhcha.weaver.courier.CourierFolder
import io.github.junhcha.weaver.courier.CourierRuntime
import io.github.junhcha.weaver.inbox.InboxRuntime
import io.github.junhcha.weaver.schema.Proposal
import io.github.junhcha.weaver.schema.Verdict
import io.github.junhcha.weaver.storage.AppStorage
import java.io.File

/** IO entry points; serializes UI actions and WorkManager with Courier's process lock. */
object ReviewRuntime {
    private fun store(context: Context) = ReviewStore(File(AppStorage.exchangeDir(context), "reviews"))
    fun records(context: Context): List<ReviewRecord> = synchronized(CourierRuntime.lock) { store(context).records() }
    fun recordsV2(context: Context): List<ReviewRecordV2> = synchronized(CourierRuntime.lock) { store(context).recordsV2() }
    fun open(context: Context, id: String): Proposal = synchronized(CourierRuntime.lock) {
        val p = store(context).latest(CourierRuntime.folder(context), id)
        val known = InboxRuntime.proposals(context).firstOrNull { it.proposalId == id }
        check(known == null || p.rev >= known.rev) { "최신 rev의 동기화를 기다리세요" }
        p
    }
    fun submit(context: Context, proposal: Proposal, verdict: Verdict) = synchronized(CourierRuntime.lock) {
        check(open(context, proposal.proposalId).rev == proposal.rev) { "새 rev가 도착했습니다. 다시 확인하세요" }
        store(context).queue(CourierRuntime.folder(context), proposal, verdict)
        InboxRuntime.submitted(context, proposal.proposalId, proposal.rev)
        CourierRuntime.kick(context)
        InboxRuntime.refresh(context)
    }
    fun openPackage(context: Context, id: String): io.github.junhcha.weaver.schema.v2.ReviewPackage = synchronized(CourierRuntime.lock) {
        val p = store(context).latestPackage(CourierRuntime.folder(context), id)
        val known = InboxRuntime.proposals(context).firstOrNull { it.proposalId == id }
        check(known == null || p.revision >= known.rev)
        p
    }
    fun submit(context: Context, p: io.github.junhcha.weaver.schema.v2.ReviewPackage,
               verdict: io.github.junhcha.weaver.schema.v2.Verdict) = synchronized(CourierRuntime.lock) {
        check(openPackage(context, p.packageId) == p)
        store(context).queue(CourierRuntime.folder(context), p, verdict)
        InboxRuntime.submitted(context, p.packageId, p.revision)
        CourierRuntime.kick(context)
        InboxRuntime.refresh(context)
    }
    fun synchronize(context: Context, folder: CourierFolder): Boolean = synchronized(CourierRuntime.lock) {
        val store = store(context)
        InboxRuntime.proposals(context).forEach { store.observeRevision(it.proposalId, it.rev) }
        store.synchronize(folder).also { InboxRuntime.refresh(context) }
    }
    fun retry(context: Context) { CourierRuntime.kick(context) }

    /** #36 integration boundary: a fact approval is not calendar permission or an ask resolution.
     * A future calendar flow must separately check request blocks and obtain execution approval.
     * Reject/revise deliberately expose no calendar candidate.
     */
    fun calendarReviewCandidate(proposal: Proposal, verdict: Verdict): Proposal? {
        require(proposal.proposalId == verdict.proposalId && proposal.rev == verdict.rev)
        return when (verdict.verdict) {
            Verdict.Kind.APPROVE -> proposal
            Verdict.Kind.EDIT -> ReviewDraft.validateEdits(proposal, verdict.edits)
            Verdict.Kind.REJECT, Verdict.Kind.REVISE -> null
        }
    }
}
