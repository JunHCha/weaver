package io.github.junhcha.weaver.review

import io.github.junhcha.weaver.schema.ReviewPackage

fun ReviewPackage.reviewTitle(): String = title.ifBlank {
    draftBody.lineSequence().map { it.trim().trimStart('#', '-', '*', ' ') }
        .firstOrNull { it.isNotBlank() && it !in setOf("요약", "통화 요약", "대화 요약") }
        ?.take(100) ?: "내용을 확인해 주세요"
}

fun ReviewPackage.sourceLabel(): String = when (sourceType.lowercase()) {
    "call", "audio", "call_transcript" -> "통화"
    "voice_memo" -> "음성 메모"
    "kakao", "kakaotalk" -> "카카오톡"
    "text", "share_text" -> "텍스트"
    else -> "문서"
}

fun ReviewStatus.reviewLabel(): String = when (this) {
    ReviewStatus.PENDING, ReviewStatus.SENT, ReviewStatus.INFERRED -> "답변을 반영하고 있습니다"
    ReviewStatus.ERROR -> "답변을 보내지 못했습니다 · 다시 시도해 주세요"
    ReviewStatus.CONFLICT -> "다른 답변이 있습니다 · 최신 문서를 확인해 주세요"
    ReviewStatus.STALE -> "새 내용이 있습니다 · 최신 문서를 확인해 주세요"
}
