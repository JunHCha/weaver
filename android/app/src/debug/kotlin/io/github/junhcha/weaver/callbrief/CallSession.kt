package io.github.junhcha.weaver.callbrief

/** Handles the duplicate blank/number PHONE_STATE broadcasts without reopening a strip. */
internal class CallSession {
    var state = "IDLE"
        private set
    var number: String? = null
        private set

    fun accept(next: String, incoming: String?): Boolean {
        if (next !in setOf("IDLE", "RINGING", "OFFHOOK")) return false
        if (state == "IDLE" && next != "IDLE") number = null
        val numberChanged = !incoming.isNullOrBlank() && incoming != number
        if (numberChanged) number = incoming
        if (next == state) return numberChanged && state != "IDLE"
        val wasActive = state != "IDLE"
        state = next
        return next != "IDLE" || wasActive
    }
}

internal object FictionalBrief {
    const val title = "가상 거래처 · 솔빛 공방"
    const val body = "시험용 가상 자료 · 보기 전용\n\n지난 대화: 견본 색상을 함께 검토했습니다.\n확인할 일: 다음 주 견본 전달 일정.\n약속: 수요일 오후에 일정만 다시 확인합니다.\n\n실제 인물·서버·Fabric 자료와 무관합니다."
}
