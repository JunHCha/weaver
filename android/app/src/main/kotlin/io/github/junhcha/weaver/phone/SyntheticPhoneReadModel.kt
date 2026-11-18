package io.github.junhcha.weaver.phone

import java.time.Clock
import java.time.Instant
import java.time.LocalDate
import java.time.LocalTime

/**
 * Made-up data for the phone screens (stage 1). Dates are relative to [clock] in Asia/Seoul so the "지금" screen
 * always has something overdue, due today and recent. Nothing here touches a real vault or the network.
 */
class SyntheticPhoneReadModel(private val clock: Clock = Clock.systemUTC()) : PhoneReadModel {
    override val isExample: Boolean = true

    override suspend fun snapshot(): PhoneSnapshot {
        val today = clock.instant().atZone(SeoulZone).toLocalDate()
        fun at(daysAgo: Long, hour: Int, minute: Int = 0): Instant =
            today.minusDays(daysAgo).atTime(LocalTime.of(hour, minute)).atZone(SeoulZone).toInstant()
        var seq = 0L
        fun thread(
            id: String, kind: ThreadKind, content: String, daysAgo: Long, hour: Int, source: String,
            counterpart: String? = null, tags: List<String> = emptyList(), direction: Direction? = null,
            deadline: LocalDate? = null, scheduleAt: Instant? = null, field: String? = null,
            presentation: Presentation? = Presentation.FACT, resolves: String? = null, retracts: String? = null,
            executes: String? = null, updates: String? = null, minute: Int = 0,
        ) = PhoneThread(
            id, ++seq, kind, content, at(daysAgo, hour, minute), at(daysAgo, hour, minute + 1), source, counterpart,
            tags, direction, deadline, scheduleAt, field, presentation, resolves, retracts, updates, executes,
        )

        val threads = listOf(
            thread("t-10", ThreadKind.SCHEDULE, "장비 교체 현장 방문", 8, 11, "통화", "이서준",
                listOf("협력사 A", "네트워크", "방화벽 장비 교체"), scheduleAt = today.plusDays(3).atTime(10, 0).atZone(SeoulZone).toInstant()),
            thread("t-12", ThreadKind.REQUEST, "VPN 계정 만료 연장", 6, 15, "통화", "이서준",
                listOf("네트워크"), Direction.RECEIVED, deadline = today.minusDays(5)),
            thread("t-12-done", ThreadKind.EXECUTION, "VPN 계정 만료일 연장 완료", 5, 9, "통화", "이서준",
                listOf("네트워크"), presentation = Presentation.HISTORY, resolves = "t-12"),
            thread("t-13", ThreadKind.RECORD, "장비 반입 시 이서준 동행 필요", 5, 16, "메모", "한지우",
                listOf("이서준", "네트워크")),
            thread("t-14a", ThreadKind.RECORD, "스위치 펌웨어 일정 협의", 4, 10, "통화", "이서준", listOf("네트워크")),
            thread("t-14b", ThreadKind.REQUEST, "방화벽 룰 3건 반영 요청", 4, 11, "통화", "이서준",
                listOf("협력사 A", "네트워크", "방화벽 장비 교체"), Direction.RECEIVED, deadline = today.plusDays(14)),
            thread("t-14b-edit", ThreadKind.REQUEST, "마감 변경", 3, 9, "통화", "이서준", listOf("방화벽 장비 교체"),
                deadline = today.plusDays(10), field = "deadline", presentation = Presentation.HISTORY, updates = "t-14b"),
            thread("t-17", ThreadKind.REQUEST, "협력사 A 견적서 회신", 1, 10, "통화", "이서준",
                listOf("협력사 A", "네트워크"), Direction.RECEIVED, deadline = today.minusDays(1)),
            thread("t-sw", ThreadKind.SCHEDULE, "스위치 교체 사전 점검", 1, 18, "통화", "한지우",
                listOf("네트워크"), scheduleAt = today.atTime(15, 0).atZone(SeoulZone).toInstant()),
            thread("t-pr", ThreadKind.REQUEST, "임원실 프린터 드라이버 배포", 2, 13, "통화", "한지우",
                listOf("총무"), Direction.RECEIVED, deadline = today),
            thread("t-vpn", ThreadKind.DECISION, "VPN 장비는 2주 뒤 교체로 확정", 1, 17, "통화", "이서준",
                listOf("네트워크", "방화벽 장비 교체"), minute = 20),
            thread("t-card", ThreadKind.RECORD, "출입 카드 발급 절차 문의", 1, 14, "통화", "박민아",
                listOf("총무"), minute = 5),
            thread("t-old", ThreadKind.RECORD, "UPS 점검 일정 정리", 12, 10, "메모", null, listOf("네트워크")),
        )
        val tags = listOf(
            PhoneTag("이서준", TagKind.PERSON), PhoneTag("한지우", TagKind.PERSON), PhoneTag("박민아", TagKind.PERSON),
            PhoneTag("협력사 A", TagKind.ORGANIZATION),
            PhoneTag("네트워크", TagKind.AREA), PhoneTag("총무", TagKind.AREA),
            PhoneTag("방화벽 장비 교체", TagKind.PROJECT),
        )
        val wiki = listOf(
            WikiPage(
                "w-fw", "방화벽 장비 교체", TagKind.PROJECT, "네트워크", today.minusDays(1),
                WikiFrontmatter("진행 중", today.plusDays(10), listOf("나", "이서준"), "현장 방문 ${today.plusDays(3).monthValue}/${today.plusDays(3).dayOfMonth}"),
                """
                노후 장비를 신규 모델로 교체한다. 룰 이관은 [[협력사 A]]가 맡고, 교체 당일 30분 단절을 공지한다.

                ## 진행
                - [[이서준]]과 방화벽 룰 3건 반영 일정 협의
                - **VPN 장비**는 2주 뒤 별도로 교체하기로 했다.

                남은 일: 룰 3건 반영 확인, 현장 출입 신청. 영역은 [[네트워크]].
                """.trimIndent(),
            ),
            WikiPage(
                "w-net", "네트워크", TagKind.AREA, null, today.minusDays(2),
                WikiFrontmatter("운영 중", null, listOf("나"), null),
                "사내 유무선망과 VPN, 방화벽을 맡는 영역이다.\n\n- 진행 프로젝트: [[방화벽 장비 교체]]\n- 주요 상대: [[이서준]], [[한지우]]",
            ),
            WikiPage(
                "w-ga", "총무", TagKind.AREA, null, today.minusDays(4),
                WikiFrontmatter("운영 중", null, listOf("나", "박민아"), "프린터 드라이버 배포"),
                "출입 카드, 사무기기, 비품을 맡는 영역이다. 담당은 [[박민아]].",
            ),
        )
        return PhoneSnapshot(threads, tags, wiki)
    }
}
