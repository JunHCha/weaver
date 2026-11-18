package io.github.junhcha.weaver.phone

import io.github.junhcha.weaver.transport.FakeMobileServer
import io.github.junhcha.weaver.transport.HttpsMobileApi
import java.time.Instant
import java.time.LocalDate
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class HttpPhoneReadModelTest {
    @get:Rule val tmp = TemporaryFolder()
    private val server = FakeMobileServer()
    @After fun stop() = server.close()

    private fun thread(
        id: String, kind: String = "요청", seq: Int = 1, extra: String = "", tags: String = "[\"네트워크\"]",
        presentation: String? = "fact",
    ) = """{"thread_id":"$id","seq":$seq,"kind":"$kind","content":"내용 $id","occurred_at":"2026-11-10T01:00:00Z",""" +
        """"recorded_at":"2026-11-10T01:01:00Z","counterpart":"이서준","tags":$tags,"field":null,"value":null,""" +
        """"source_id":"src_1","actor":"human","direction":"받음","resolves":null,"retracts":null,"updates":null,""" +
        """"blocked_by":null,"executes":null,"correction":null,"source":"통화"${presentation?.let { ",\"presentation\":\"$it\"" } ?: ""}$extra}"""

    private fun seed() {
        server.threads = listOf(
            thread("t1", extra = ""","deadline":null""").replace("\"field\":null,\"value\":null", "\"field\":\"deadline\",\"value\":\"2026-11-12\""),
            thread("t2", "기록", 2),
            thread("t3", "태그정정", 3, presentation = "history").replace("\"correction\":null", "\"correction\":{\"target\":\"t1\",\"add\":[\"VPN\"],\"remove\":[]}"),
        )
        server.tags = listOf("""{"name":"네트워크","kind":"topic","archived":false}""", """{"name":"이서준","kind":"person"}""")
        server.wiki = listOf("""{"page_id":"w1","title":"방화벽 장비 교체","kind":"project","area":"네트워크","updated":"2026-11-09",
            "frontmatter":{"status":"진행","deadline":"2026-11-30","assignees":["한지우"],"next":"장비 반입"},"body":"# 방화벽\n[[이서준]] 동행"}""")
    }

    private fun model(cache: java.io.File = tmp.newFile("snapshot.json").also { it.delete() }, live: Boolean = true) =
        HttpPhoneReadModel({ if (live) HttpsMobileApi(FakeMobileServer.pairing(server), connectTimeoutMs = 500, readTimeoutMs = 1000) else null }, cache)

    @Test fun readsThreadsTagsAndWikiFromTheServerWithTheWebRules() = runBlocking {
        seed()
        val snapshot = model().snapshot()
        assertEquals(3, snapshot.threads.size)
        val t1 = snapshot.threads.first { it.id == "t1" }
        assertEquals(ThreadKind.REQUEST, t1.kind)
        assertEquals(LocalDate.parse("2026-11-12"), t1.deadline)
        assertEquals(Direction.RECEIVED, t1.direction)
        assertEquals("이서준", t1.counterpart)
        assertEquals("통화", t1.source)
        assertEquals(Instant.parse("2026-11-10T01:00:00Z"), t1.occurredAt)
        val correction = snapshot.threads.first { it.id == "t3" }
        assertTrue("explicit presentation drives fact/history", correction.isHistory())
        assertEquals(TagCorrection("t1", listOf("VPN"), emptyList()), correction.correction)
        assertEquals(TagKind.PERSON, snapshot.tags.first { it.name == "이서준" }.kind)
        val page = snapshot.wikiPages.single()
        assertEquals("방화벽 장비 교체", page.title)
        assertEquals(TagKind.PROJECT, page.kind)
        assertEquals(listOf("한지우"), page.frontmatter.assignees)
        assertEquals(LocalDate.parse("2026-11-30"), page.frontmatter.deadline)
        assertTrue(page.body.contains("[[이서준]]"))
    }

    @Test fun followsEveryPageOfTheServersCursor() = runBlocking {
        server.threads = (1..5).map { thread("t$it", seq = it) }
        server.pageSize = 2
        assertEquals(5, model().snapshot().threads.size)
    }

    @Test fun showsTheLastCopyWhenTheServerCannotBeReached() = runBlocking {
        seed()
        val cache = tmp.newFile("snapshot.json").also { it.delete() }
        val online = model(cache)
        assertEquals(3, online.snapshot().threads.size)
        assertNull(online.offlineSince)
        val offline = model(cache, live = false)
        val snapshot = offline.snapshot()
        assertEquals("the cached copy is shown", 3, snapshot.threads.size)
        assertEquals(1, snapshot.wikiPages.size)
        assertNotNull(offline.offlineSince)
    }

    @Test fun aPartialFetchNeverReplacesTheCacheOrReadsAsDeletion() = runBlocking {
        seed()
        val cache = tmp.newFile("snapshot.json").also { it.delete() }
        model(cache).snapshot()
        server.threads = (1..6).map { thread("n$it", seq = it) }
        server.failThreadsFrom = "2"
        val partial = model(cache)
        val snapshot = partial.snapshot()
        assertEquals(setOf("t1", "t2", "t3"), snapshot.threads.map { it.id }.toSet())
        assertNotNull(partial.offlineSince)
    }

    @Test fun serverRebootBetweenPagesKeepsTheLastCompleteCopy() = runBlocking {
        seed()
        val cache = tmp.newFile("snapshot.json").also { it.delete() }
        model(cache).snapshot()
        server.offline = true
        val snapshot = model(cache).snapshot()
        assertEquals(3, snapshot.threads.size)
    }

    @Test fun noCacheAndNoServerIsEmptyNotAnError() = runBlocking {
        val snapshot = model(live = false).snapshot()
        assertTrue(snapshot.isEmpty)
    }

    @Test fun anUnknownKindDropsOnlyThatRowNotTheSnapshot() = runBlocking {
        server.threads = listOf(thread("t1"), thread("bad", "미지"))
        server.tags = emptyList(); server.wiki = emptyList()
        assertEquals(listOf("t1"), model().snapshot().threads.map { it.id })
    }
}
