package io.github.junhcha.weaver.phone

import io.github.junhcha.weaver.transport.ContractFixtures
import io.github.junhcha.weaver.transport.FakeMobileServer
import io.github.junhcha.weaver.transport.HttpsMobileApi
import java.time.Instant
import java.time.LocalDate
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class HttpPhoneReadModelTest {
    @get:Rule val tmp = TemporaryFolder()
    private val server = FakeMobileServer()
    @After fun stop() { server.close(); assertEquals(emptyList<String>(), server.mismatches.toList()) }

    private val pages = arrayOf("read.threads_first_page", "read.threads_next_page", "read.threads_last_page")
    private fun serveReads() { server.serve(*pages, "read.tags", "wiki.list") }

    private fun model(cache: java.io.File = tmp.newFile("snapshot.json").also { it.delete() }, live: Boolean = true,
                      onProblem: (String?) -> Unit = {}) =
        HttpPhoneReadModel({ if (live) HttpsMobileApi(FakeMobileServer.pairing(server), connectTimeoutMs = 500, readTimeoutMs = 1000) else null }, cache, onProblem = onProblem)

    @Test fun readsThreadsTagsAndWikiAsTheServerComputedThem() = runBlocking {
        serveReads()
        val snapshot = model().snapshot()
        assertEquals(10, snapshot.threads.size)
        assertEquals(listOf("GET /mobile/v1/read/threads", "GET /mobile/v1/read/threads", "GET /mobile/v1/read/threads",
            "GET /mobile/v1/read/tags", "GET /mobile/v1/read/wiki/pages"), server.requests)
        val t1 = snapshot.threads.first { it.id == "t1" }
        assertEquals(ThreadKind.REQUEST, t1.kind)
        assertEquals("the server's effective_deadline", LocalDate.parse("2026-11-10"), t1.deadline)
        assertEquals(Direction.RECEIVED, t1.direction); assertEquals("이서준", t1.counterpart)
        assertEquals(true, t1.open); assertEquals(false, t1.retracted); assertEquals(listOf("협력사 A"), t1.mentions)
        val correction = snapshot.threads.first { it.id == "c1" }
        assertTrue("the server's presentation drives fact/history", correction.isHistory())
        assertEquals(TagCorrection("t1", listOf("방화벽 교체"), emptyList()), correction.correction)
        assertEquals(TagKind.PERSON, snapshot.tags.first { it.name == "이서준" }.kind)
        val page = snapshot.wikiPages.first { it.title == "방화벽 교체" }
        assertEquals(TagKind.PROJECT, page.kind)
        assertEquals(listOf("나"), page.frontmatter.assignees)
        assertEquals(LocalDate.parse("2026-11-30"), page.frontmatter.deadline)
        assertEquals("the list has no body; it is fetched when the page opens", "", page.body)
    }

    @Test fun threadValuesAreNotRecomputedFromHistoryRows() = runBlocking {
        serveReads()
        val snapshot = model().snapshot()
        // c1 (태그정정) adds 방화벽 교체 to t1; the server already put it into t1.tags, applying it again must change nothing.
        assertSame(snapshot, snapshot.effective())
        assertTrue("방화벽 교체" in snapshot.threads.first { it.id == "t1" }.tags)
        val now = nowView(snapshot, Instant.parse("2026-11-12T00:00:00Z"))
        assertTrue("the open flag from the server decides overdue", now.overdue.any { it.thread.id == "t1" })
    }

    @Test fun wikiBodyIsFetchedWhenOpenedCachedAndAvailableOffline() = runBlocking {
        serveReads(); server.serve("wiki.page")
        val cache = tmp.newFile("snapshot.json").also { it.delete() }
        val online = model(cache)
        val page = online.snapshot().wikiPages.first { it.title == "방화벽 교체" }
        val body = online.wikiBody(page)
        assertEquals("노후 장비를 교체한다.\n\n## 진행\n- 룰 3건\n\n## 내 메모\n", body)
        server.offline = true
        assertEquals("a page opened once opens offline", body, model(cache, live = false).wikiBody(page))
        server.offline = false
        // A current copy is revalidated with the stored ETag and the server answers 304.
        server.reset("wiki.page"); server.serve("wiki.page_not_modified")
        assertEquals(body, model(cache).wikiBody(page))
        assertEquals("\"${page.sha256}\"", server.seenAuth.size.let { ContractFixtures["wiki.page"].responseHeaders["etag"] })
    }

    @Test fun showsTheLastCopyWhenTheServerCannotBeReached() = runBlocking {
        serveReads()
        val cache = tmp.newFile("snapshot.json").also { it.delete() }
        val online = model(cache)
        assertEquals(10, online.snapshot().threads.size)
        assertNull(online.offlineSince)
        server.offline = true
        val offline = model(cache)
        val snapshot = offline.snapshot()
        assertEquals("the cached copy is shown", 10, snapshot.threads.size)
        assertEquals(2, snapshot.wikiPages.size)
        assertNotNull(offline.offlineSince)
    }

    @Test fun aPartialFetchNeverReplacesTheCacheOrReadsAsDeletion() = runBlocking {
        serveReads()
        val cache = tmp.newFile("snapshot.json").also { it.delete() }
        model(cache).snapshot()
        server.reset(*pages); server.serve(pages[0]) // the second page (cursor c4) is no longer served: 404
        val partial = model(cache)
        val snapshot = partial.snapshot()
        assertEquals("the last complete copy stays", 10, snapshot.threads.size)
        assertNotNull(partial.offlineSince)
    }

    @Test fun noCacheAndNoServerIsEmptyNotAnError() = runBlocking {
        assertTrue(model(live = false).snapshot().isEmpty)
    }

    @Test fun anUnknownThreadKindIsSkippedButCountedForTheConnectionCard() = runBlocking {
        server.serveDerived(pages[0]) { body -> mutateFirstKind(body, "미지") }
        server.serve(pages[1], pages[2], "read.tags", "wiki.list")
        var problem: String? = "unset"
        val snapshot = model(onProblem = { problem = it }).snapshot()
        assertEquals(9, snapshot.threads.size)
        assertEquals("unknown_thread_kind:1", problem)
    }

    private fun mutateFirstKind(body: kotlinx.serialization.json.JsonElement, kind: String): kotlinx.serialization.json.JsonElement {
        val root = body.jsonObject
        val items = (root["items"] as JsonArray).toMutableList()
        items[0] = JsonObject(items[0].jsonObject.toMutableMap().also { it["kind"] = JsonPrimitive(kind) })
        return JsonObject(root.toMutableMap().also { it["items"] = JsonArray(items) })
    }
}
