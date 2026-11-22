package io.github.junhcha.weaver.transport

import io.github.junhcha.weaver.courier.CourierClient
import io.github.junhcha.weaver.pipeline.PipelineCache
import io.github.junhcha.weaver.schema.PipelineItem
import java.io.File
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonPrimitive
import org.junit.After
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** The new read calls and the checkpoint PUT, exactly as the contract fixtures record them. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class FeedApiContractTest {
    @get:Rule val tmp = TemporaryFolder()
    private val server = FakeMobileServer()
    private val api by lazy { HttpsMobileApi(FakeMobileServer.pairing(server), connectTimeoutMs = 1000, readTimeoutMs = 1000) }
    private val id = "src_00000000000000000000000001"
    private val kinds = setOf("review", "action", "alert", "pipeline", "wiki", "fabric")

    @After fun stop() { server.close(); assertEquals(emptyList<String>(), server.mismatches.toList()) }

    @Test fun changesNamesItsKindsAndDecodesTheNegotiatedSnapshot() {
        server.serve("changes_kinds.negotiated_snapshot")
        val feed = api.changes(null, kinds, 100)
        assertEquals(setOf("fabric", "alert", "pipeline", "wiki"), feed.items.map { it.kind }.toSet())
        assertEquals(Fx.json("changes_kinds.negotiated_snapshot")["next_cursor"]!!.jsonPrimitive.content, feed.nextCursor)
        assertFalse(feed.hasMore)
    }

    @Test fun anAdditiveFieldInTheEnvelopeDoesNotStopTheFeed() {
        server.serveDerived("changes_kinds.negotiated_snapshot") { JsonObject((it as JsonObject) + ("later_field" to JsonPrimitive(1))) }
        assertEquals(4, api.changes(null, kinds, 100).items.size)
    }

    @Test fun anEnvelopeThatIsNotAFeedIsAFormatErrorNotACrash() {
        server.serveDerived("changes_kinds.negotiated_snapshot") { JsonObject(mapOf("items" to JsonPrimitive("x"))) }
        assertThrows(InvalidResponseException::class.java) { api.changes(null, kinds, 100) }
    }

    @Test fun aStaleOrForeignCursorIsA410TheFeedRestartsFrom() {
        server.serve("changes.cursor_expired", "changes_kinds.kinds_changed_cursor")
        val expired = ContractFixtures["changes.cursor_expired"].query.getValue("cursor")
        val error = assertThrows(ApiException::class.java) { api.changes(expired, setOf("fabric"), 100) }
        assertEquals(410 to "cursor_expired", error.status to error.code)
        val other = ContractFixtures["changes_kinds.kinds_changed_cursor"].query.getValue("cursor")
        assertEquals(410, assertThrows(ApiException::class.java) { api.changes(other, setOf("fabric"), 100) }.status)
    }

    @Test fun checkpointSendsOnlyTheCursorAndChecksTheEcho() {
        server.serve("changes.checkpoint")
        api.putCheckpoint(ContractFixtures["changes.checkpoint"].requestBody!!.let { (it as JsonObject)["cursor"]!!.jsonPrimitive.content })
    }

    @Test fun pipelineListItemNotModifiedAndNotFound() {
        server.serve("pipeline.list", "pipeline.item", "pipeline.item_not_modified", "pipeline.item_not_found", "pipeline.files_route")
        val rows = api.readPipeline(null, 500)
        assertEquals(1, rows.items.size)
        CourierClient.decoded<PipelineItem>(rows.items.single().toString())
        val etag = ContractFixtures["pipeline.item"].responseHeaders.getValue("etag")
        val fetched = api.readPipelineItem(id, null)!!
        assertEquals(etag, fetched.etag)
        assertNull("304: the cached body is still current", api.readPipelineItem(id, etag))
        assertEquals("pipeline_not_found", assertThrows(ApiException::class.java) { api.readPipelineItem("missing", null) }.code)
    }

    @Test fun aSourceIdThatCouldEscapeThePathIsRefusedBeforeAnyRequest() {
        assertThrows(InvalidResponseException::class.java) { api.readPipelineItem("../captures", null) }
        assertTrue(server.requests.isEmpty())
    }

    @Test fun theCachedBodyComesBackAsTheStrictModelAndSurvivesAReopen() {
        val dir = File(tmp.newFolder(), "cache")
        val cache = PipelineCache(dir)
        val raw = Fx.json("pipeline.item")
        assertTrue(cache.put(id, raw, "\"e\"", 5))
        val reopened = PipelineCache(dir)
        assertEquals(CourierClient.decoded<PipelineItem>(raw.toString()), reopened.items().single())
        assertEquals("\"e\"", reopened.cached(id)!!.etag)
    }
}
