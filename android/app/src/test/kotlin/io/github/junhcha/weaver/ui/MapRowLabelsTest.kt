package io.github.junhcha.weaver.ui

import org.junit.Assert.assertEquals
import org.junit.Test

class MapRowLabelsTest {
    @Test fun threadTitleKeepsOnlyFirstSentence() {
        assertEquals("다음 주에 점검합니다.", threadTitle("  다음 주에 점검합니다. 결과를 공유합니다.  "))
        assertEquals("확인했나요?", threadTitle("확인했나요? 다시 연락합니다."))
        assertEquals("완료！", threadTitle("완료！ 다음 단계"))
    }
    @Test fun threadTitleHandlesParagraphsBlankAndUnpunctuatedContent() {
        assertEquals("첫 줄", threadTitle("첫 줄\r\n다음 줄"))
        assertEquals("내용 없음", threadTitle(" \n\t"))
        assertEquals("점검 결과 공유", threadTitle("점검  결과\t공유"))
        assertEquals("버전 1.2를 배포합니다.", threadTitle("버전 1.2를 배포합니다. 확인 바랍니다."))
    }
    @Test fun longFirstSentenceIsPreservedForTwoLineLayout() {
        val sentence = "장비 점검 결과를 공유합니다 ".repeat(60).trim()
        assertEquals(sentence, threadTitle(sentence))
    }
    @Test fun wikiTitleRemovesFoldersAndOnlyFinalMarkdownSuffix() {
        assertEquals("장비 점검", wikiPageTitle("pages/장비 점검.md"))
        assertEquals("회의.md 기록", wikiPageTitle("pages/영역/회의.md 기록.md"))
        assertEquals("2026-11-18", wikiPageTitle("Digest/2026/11/2026-11-18.md"))
        assertEquals("이름", wikiPageTitle("이름.md"))
    }
    @Test fun pageKindUsesOnlyExistingPathInformation() {
        assertEquals("Digest", wikiPageKind("Digest/2026/11/2026-11-18.md"))
        assertEquals("출처 요약", wikiPageKind("summaries/가상 통화.md"))
        assertEquals("Wiki 페이지", wikiPageKind("pages/가상 영역.md"))
    }
    @Test fun itemTitleFallsBackOnlyWhenMissing() {
        assertEquals("가상 통화 상대", sourceTitle("가상 통화 상대"))
        assertEquals("저장한 항목", sourceTitle(" \t"))
    }

    @Test fun firstSentenceStripsLeadingListAndQuoteMarkers() {
        for (prefix in listOf("1. ", "- ", "> ", "> 1. ", "12. ")) {
            assertEquals("점검합니다.", threadTitle(prefix + "점검합니다. 다음 문장입니다."))
        }
        assertEquals("내용 없음", threadTitle("1."))
        assertEquals("내용 없음", threadTitle("> 23. "))
    }
    @Test fun firstSentenceKeepsClosingQuotesAndBrackets() {
        assertEquals("“점검합니다.”", threadTitle("“점검합니다.” 다음 문장입니다."))
        assertEquals("(점검합니다.)", threadTitle("(점검합니다.) 다음 문장입니다."))
        assertEquals("[점검합니다.]", threadTitle("[점검합니다.] 다음 문장입니다."))
        assertEquals("\"점검합니다.\"", threadTitle("\"점검합니다.\" 다음 문장입니다."))
    }
    @Test fun firstSentenceRecognizesEllipsisAndNonbreakingSpace() {
        assertEquals("기다립니다…", threadTitle("기다립니다… 다음 문장입니다."))
        assertEquals("기다립니다…", threadTitle("기다립니다…다음 문장입니다."))
        assertEquals("점검합니다.", threadTitle("점검합니다.\u00a0다음 문장입니다."))
    }
    @Test fun displayNamesNormalizeNfdWithoutChangingRawPaths() {
        val title = "한글 이름"
        val nfd = java.text.Normalizer.normalize(title, java.text.Normalizer.Form.NFD)
        val path = "pages/$nfd.md"
        assertEquals(title, wikiPageTitle(path))
        assertEquals(title, sourceTitle(nfd))
        assertEquals("$title.", threadTitle("$nfd. 다음 문장"))
        assertEquals("pages/$nfd.md", path)
        assertEquals("Wiki 페이지", wikiPageKind("질문 결과.md"))
    }
    @Test fun rowMetaOmitsSharedCardTimeAndNeverRepeatsSourceTitle() {
        val time = "2026-11-18T09:00:00+09:00"
        assertEquals("요청", threadMeta("요청", time, "2026-11-18T00:00:00Z"))
        assertEquals("요청 · 반영 ${mapTime(time)}", threadMeta("요청", time, "2026-11-18T01:00:00Z"))
        assertEquals("요청 · 반영 ${mapTime(time)}", threadMeta("요청", time, null))
    }
}
