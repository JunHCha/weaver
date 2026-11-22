package io.github.junhcha.weaver.pipeline

import io.github.junhcha.weaver.transport.Fx
import java.io.File
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class PipelineCacheTest {
    @get:Rule val tmp = TemporaryFolder()
    private lateinit var dir: File
    private lateinit var cache: PipelineCache
    private val a = "src_00000000000000000000000001"
    private val b = "src_00000000000000000000000002"

    @Before fun setup() { dir = tmp.newFolder("cache"); cache = PipelineCache(dir) }

    private fun body(id: String, revision: Int = 1) =
        JsonObject(Fx.json("pipeline.item") + ("source_id" to JsonPrimitive(id)) + ("revision" to JsonPrimitive(revision)))

    @Test fun aLowerRevisionNeverRollsTheCachedItemBack() {
        assertTrue(cache.put(a, body(a, 3), "\"3\"", 1))
        assertTrue(cache.put(a, body(a, 2), "\"2\"", 2))
        assertEquals(3L, cache.cached(a)!!.revision)
        assertEquals("\"3\"", cache.cached(a)!!.etag)
    }

    @Test fun anInvalidBodyOrIdChangesNothing() {
        assertTrue(cache.put(a, body(a), null, 1))
        assertFalse(cache.put(a, JsonObject(mapOf("source_id" to JsonPrimitive(a))), null, 2))
        assertFalse("the body must belong to the id it is stored under", cache.put(b, body(a), null, 2))
        assertFalse(cache.put("../x", body(a), null, 2))
        assertEquals(listOf(a), cache.items().map { it.sourceId })
        assertNull(cache.cached("../x"))
    }

    @Test fun oneDamagedFileDoesNotHideTheOthers() {
        cache.put(a, body(a), null, 1); cache.put(b, body(b), null, 1)
        File(dir, "items/$a.json").writeText("not json")
        assertEquals(listOf(b), cache.items().map { it.sourceId })
        assertEquals(2, cache.status().items)
    }

    @Test fun announcedItemsStayApplyingUntilTheirBodyArrivesOrTheyAreGone() {
        cache.markApplying(a); cache.markApplying(b)
        assertEquals(setOf(a, b), cache.status().applying)
        cache.put(a, body(a), null, 5)
        assertEquals(setOf(b), cache.status().applying)
        cache.settled(b)
        assertTrue(cache.status().applying.isEmpty())
        assertEquals(5L, cache.status().syncedAt)
    }

    @Test fun onlyATombstoneOrACompleteListRemovesItems() {
        cache.put(a, body(a), null, 1); cache.put(b, body(b), null, 1)
        cache.remove(a, 2)
        assertEquals(listOf(b), cache.items().map { it.sourceId })
        // A listing keeps an item whose row this build could not read, drops the rest, and may lower a revision.
        cache.put(a, body(a, 5), null, 3)
        val removed = cache.replaceAll(listOf(Triple(a, body(a, 1), null)), unreadable = setOf(b), now = 4)
        assertEquals(0, removed)
        assertEquals(1L, cache.cached(a)!!.revision)
        assertEquals(2, cache.replaceAll(emptyList(), emptySet(), 5))
        assertTrue(cache.items().isEmpty())
    }

    @Test fun aRowWithoutAUsableIdIsSkippedByTheListing() {
        assertNull(PipelineCache.idOf(JsonObject(mapOf("source_id" to JsonPrimitive("../x")))))
        assertNull(PipelineCache.idOf(JsonObject(emptyMap())))
    }
}
