package io.github.junhcha.weaver.review

import io.github.junhcha.weaver.schema.Stage

enum class StepStatus(val label: String) { DONE("완료"), CURRENT("진행 중"), UPCOMING("예정") }

data class ProgressStep(val stage: Stage, val status: StepStatus, val time: String?)
data class ReviewProgress(val steps: List<ProgressStep>, val response: String?)

fun reviewProgress(stage: Stage, times: Map<Stage, String>, delivery: ReviewStatus?): ReviewProgress {
    val steps = Stage.entries.map { step ->
        ProgressStep(step, when {
            step.ordinal < stage.ordinal -> StepStatus.DONE
            step == stage -> StepStatus.CURRENT
            else -> StepStatus.UPCOMING
        }, times[step])
    }
    val response = when (delivery) {
        null -> null
        ReviewStatus.SENT -> if (stage.ordinal >= Stage.APPLY.ordinal) "서버 반영" else "전송됨 · 서버 반영 대기"
        ReviewStatus.INFERRED -> if (stage.ordinal >= Stage.APPLY.ordinal) "서버 반영" else "서버 반영 추정"
        else -> delivery.label
    }
    return ReviewProgress(steps, response)
}

fun Stage.displayName(): String = when (this) {
    Stage.COLLECT -> "수집"
    Stage.SEND -> "전송"
    Stage.ORGANIZE -> "정리"
    Stage.QUESTION -> "질문"
    Stage.REVIEW -> "검토"
    Stage.APPLY -> "반영"
    Stage.UPDATE_WIKI -> "wiki갱신"
}
