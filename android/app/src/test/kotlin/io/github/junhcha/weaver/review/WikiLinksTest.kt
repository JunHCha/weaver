package io.github.junhcha.weaver.review

import org.junit.Assert.*
import org.junit.Test

class WikiLinksTest {
    @Test fun parsesNamesAndAliasesAndEncodesUri() {
        val links = wikiLinks("전후 [[계획/회의 노트|회의]] 와 [[일정]]", "내 볼트")
        assertEquals(listOf("회의", "일정"), links.map { it.label })
        assertEquals("obsidian://open?vault=%EB%82%B4%20%EB%B3%BC%ED%8A%B8&file=%EA%B3%84%ED%9A%8D%2F%ED%9A%8C%EC%9D%98%20%EB%85%B8%ED%8A%B8", links.first().uri)
    }

    @Test fun emptyVaultDisablesLinksAndInvalidNamesAreIgnored() {
        assertNull(wikiLinks("[[문서]]", "  ").single().uri)
        assertTrue(wikiLinks("[[]] [[|별명]]", "볼트").isEmpty())
    }
}
