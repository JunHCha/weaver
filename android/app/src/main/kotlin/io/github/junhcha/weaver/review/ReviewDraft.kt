package io.github.junhcha.weaver.review

import io.github.junhcha.weaver.schema.ReviewPackage
import io.github.junhcha.weaver.schema.Verdict

object ReviewDraft {
    fun verdict(p: ReviewPackage, answers: List<String>, action: Verdict.Action, body: String, feedback: String): Verdict {
        require(answers.size == p.questions.size && answers.all { it.isNotBlank() })
        if (action == Verdict.Action.EDIT) require(body.isNotBlank())
        return Verdict(2, p.packageId, p.revision, action, answers,
            body.takeIf { action == Verdict.Action.EDIT }, feedback.takeIf { it.isNotBlank() && action != Verdict.Action.REJECT })
    }
}
