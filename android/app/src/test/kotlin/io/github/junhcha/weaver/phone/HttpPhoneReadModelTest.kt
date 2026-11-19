package io.github.junhcha.weaver.phone

import io.github.junhcha.weaver.transport.ContractFixtures
import io.github.junhcha.weaver.transport.FakeMobileServer
import io.github.junhcha.weaver.transport.Fx
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

    private val pages = Fx.threadPages.toTypedArray()
    private fun serveReads() { server.serve(*pages, "read.tags", "wiki.list") }

    private fun model(cache: java.io.File = tmp.newFile("snapshot.json").also { it.delete() }, live: Boolean = true,
                      onProblem: (String?) -> Unit = {}) =
        HttpPhoneReadModel({ if (live) HttpsMobileApi(FakeMobileServer.pairing(server), connectTimeoutMs = 500, readTimeoutMs = 1000) else null }, cache, onProblem = onProblem)

    @Test fun readsThreadsTagsAndWikiAsTheServerComputedThem() = runBlocking {
        serveReads()
        val snapshot = model().snapshot()
        assertEquals(Fx.threadCount, snapshot.threads.size)
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
        assertEquals(Fx.threadCount, online.snapshot().threads.size)
        assertNull(online.offlineSince)
        server.offline = true
        val offline = model(cache)
        val snapshot = offline.snapshot()
        assertEquals("the cached copy is shown", Fx.threadCount, snapshot.threads.size)
        assertEquals(2, snapshot.wikiPages.size)
        assertNotNull(offline.offlineSince)
    }

    @Test fun aPartialFetchNeverReplacesTheCacheOrReadsAsDeletion() = runBlocking {
        serveReads()
        val cache = tmp.newFile("snapshot.json").also { it.delete() }
        model(cache).snapshot()
        server.reset(*pages); server.serve(pages[0]) // the second page (the cursor after the first page) is no longer served: 404
        val partial = model(cache)
        val snapshot = partial.snapshot()
        assertEquals("the last complete copy stays", Fx.threadCount, snapshot.threads.size)
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
        assertEquals(Fx.threadCount - 1, snapshot.threads.size)
        assertEquals("unknown_thread_kind:1", problem)
    }


    @Test fun syntheticComplexShapesSurviveHttpParsing() = runBlocking {
        server.serve("read_shapes.threads", "read_shapes.tags", "read_shapes.wiki", "read_shapes.wiki_body")
        var problem: String? = "unset"
        val model = model(onProblem = { problem = it })
        val snapshot = model.snapshot()
        assertNull(problem)
        assertEquals(5, snapshot.threads.size)
        assertEquals(setOf(ThreadKind.SCHEDULE, ThreadKind.REQUEST, ThreadKind.TAG_CORRECTION,
            ThreadKind.REJECTION, ThreadKind.EXECUTION), snapshot.threads.map { it.kind }.toSet())
        val schedule = snapshot.threads.first { it.kind == ThreadKind.SCHEDULE }
        assertEquals(Instant.parse("2026-11-12T06:00:00Z"), schedule.scheduleAt)
        assertTrue(schedule.content.length > 2000)
        assertTrue(snapshot.tags.any { it.kind == TagKind.AREA })
        val page = snapshot.wikiPages.single()
        assertEquals(listOf("가상 담당자"), page.frontmatter.assignees)
        assertNull(page.frontmatter.deadline)
        assertNull(page.frontmatter.next)
        assertTrue(java.text.Normalizer.isNormalized(page.area!!, java.text.Normalizer.Form.NFD))
        assertTrue(model.wikiBody(page)!!.length > 2000)
    }

    @Test fun everySkippedItemIsCountedAcrossAllReadLists() = runBlocking {
        server.serveDerived(pages[0]) { body ->
            val root = mutateFirstKind(body, "미지").jsonObject
            val items = (root["items"] as JsonArray).toMutableList()
            items[1] = JsonObject(items[1].jsonObject.toMutableMap().also { it.remove("recorded_at") })
            JsonObject(root.toMutableMap().also { it["items"] = JsonArray(items) })
        }
        server.serve(pages[1], pages[2])
        for (name in listOf("read.tags", "wiki.list")) server.serveDerived(name) { body ->
            val root = body.jsonObject
            val items = (root["items"] as JsonArray).toMutableList()
            items[0] = kotlinx.serialization.json.JsonNull
            JsonObject(root.toMutableMap().also { it["items"] = JsonArray(items) })
        }
        var problem: String? = null
        val snapshot = model(onProblem = { problem = it }).snapshot()
        assertEquals(Fx.threadCount - 2, snapshot.threads.size)
        assertEquals((Fx.json("read.tags")["items"] as JsonArray).size - 1, snapshot.tags.size)
        assertEquals(1, snapshot.wikiPages.size)
        assertEquals("unknown_thread_kind:1,invalid_thread:1,invalid_tag:1,invalid_wiki_page:1", problem)
    }

    @Test fun failedReadWithOrWithoutCacheReportsTheCauseAndRecoveryClearsIt() = runBlocking {
        serveReads()
        val cache = tmp.newFile("saved-read.json").also { it.delete() }
        var problem: String? = null
        val model = model(cache, onProblem = { problem = it })
        model.snapshot()
        server.failWith("/read/tags", "errors.scope_denied")
        assertEquals(Fx.threadCount, model.snapshot().threads.size)
        assertNotNull(model.offlineSince)
        assertEquals("scope_denied", problem)
        assertTrue(model(onProblem = { problem = it }).snapshot().isEmpty)
        assertEquals("scope_denied", problem)
        server.clearFailures()
        assertEquals(Fx.threadCount, model.snapshot().threads.size)
        assertNull(problem)
        assertNull(model.offlineSince)
    }

    @Test fun cancellationIsPropagatedWithoutReplacingTheCache() = runBlocking {
        val client = HttpsMobileApi(FakeMobileServer.pairing(server))
        val cancelled = object : io.github.junhcha.weaver.transport.MobileApi by client {
            override fun threads(cursor: String?, limit: Int): io.github.junhcha.weaver.transport.Page {
                throw kotlinx.coroutines.CancellationException("cancelled")
            }
        }
        var problem: String? = null
        val cache = tmp.newFile("cancelled.json").also { it.delete() }
        val model = HttpPhoneReadModel({ cancelled }, cache, onProblem = { problem = it })
        try { model.snapshot(); fail("cancellation must escape") }
        catch (_: kotlinx.coroutines.CancellationException) { }
        assertFalse(cache.exists())
        assertNull(problem)
    }


    @Test fun malformedEnvelopeReportsAParseCodeWithoutReplacingTheCache() = runBlocking {
        serveReads()
        val cache = tmp.newFile("valid-read.json").also { it.delete() }
        var problem: String? = null
        val model = model(cache, onProblem = { problem = it })
        model.snapshot()
        val bytes = cache.readBytes()
        server.reset(*pages)
        server.serveDerived(pages[0]) { JsonObject(mapOf("items" to kotlinx.serialization.json.JsonNull)) }
        assertEquals(Fx.threadCount, model.snapshot().threads.size)
        assertEquals("invalid_read_response", problem)
        assertArrayEquals(bytes, cache.readBytes())
    }

    @Test fun wikiFetchErrorsAreReportedWhileTheLastBodyStaysReadable() = runBlocking {
        serveReads(); server.serve("wiki.page")
        var problem: String? = null
        val model = model(onProblem = { problem = it })
        val page = model.snapshot().wikiPages.first { it.title == "방화벽 교체" }
        val body = model.wikiBody(page)
        server.failWith("/read/wiki/pages/${page.id}", "errors.scope_denied")
        assertEquals(body, model.wikiBody(page))
        assertEquals("scope_denied", problem)
    }

    private fun mutateFirstKind(body: kotlinx.serialization.json.JsonElement, kind: String): kotlinx.serialization.json.JsonElement {
        val root = body.jsonObject
        val items = (root["items"] as JsonArray).toMutableList()
        items[0] = JsonObject(items[0].jsonObject.toMutableMap().also { it["kind"] = JsonPrimitive(kind) })
        return JsonObject(root.toMutableMap().also { it["items"] = JsonArray(items) })
    }
}
