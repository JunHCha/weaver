package io.github.junhcha.weaver.review

import io.github.junhcha.weaver.schema.v2.ReviewPackage
import io.github.junhcha.weaver.schema.v2.Verdict

object ReviewV2Draft {
    fun verdict(p: ReviewPackage, answers: List<String>, action: Verdict.Action, body: String, feedback: String): Verdict {
        require(answers.size == p.questions.size && answers.zip(p.questions).all { (a, q) -> a in q.choices })
        if (action == Verdict.Action.EDIT) require(body.isNotBlank())
        if (action == Verdict.Action.FEEDBACK) require(feedback.isNotBlank())
        return Verdict(2, p.packageId, p.revision, action, answers,
            body.takeIf { action == Verdict.Action.EDIT }, feedback.takeIf { action == Verdict.Action.FEEDBACK })
    }
}
