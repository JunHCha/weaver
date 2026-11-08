package io.github.junhcha.weaver.shepherd

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.time.ZoneId

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [26, 36])
class ShepherdTest {
    private lateinit var context: Context
    @Before fun reset() {
        context = ApplicationProvider.getApplicationContext()
        context.deleteDatabase("shepherd.sqlite3")
    }
    private fun row(size: Long = 100, modified: Long = 10) = RecordingMetadata(
        "content://media/external/audio/media/1", "친구_261108_123456.m4a", size, modified, 2_000,
    )
    @Test fun waitsThirtySecondsAndPersistsAcrossRestart() {
        ShepherdDatabase(context).use { db ->
            db.activate(1_000)
            assertTrue(db.observe(listOf(row()), 3_000, ZoneId.of("Asia/Seoul")))
            assertTrue(db.calls().none { it.status == CallStatus.UNSHARED })
        }
        ShepherdDatabase(context).use { db ->
            assertTrue(db.observe(listOf(row()), 32_999, ZoneId.of("UTC")))
            assertTrue(db.calls().none { it.status == CallStatus.UNSHARED })
            assertFalse(db.observe(listOf(row()), 33_000, ZoneId.of("UTC")))
            val call = db.pendingCalls().single()
            assertEquals("친구", call.counterpart)
            assertEquals("2026-11-08T12:34:56+09:00", call.occurredAt)
            assertTrue(db.markProcessed(call.id))
            assertFalse(db.markProcessed(call.id))
            db.observe(listOf(row()), 100_000, ZoneId.of("UTC"))
            assertTrue(db.calls().none { it.status == CallStatus.UNSHARED })
            assertEquals(CallStatus.PROCESSED, db.calls().single().status)
        }
    }
    @Test fun growthRestartsStabilityAndOldOrEmptyFilesAreExcluded() {
        ShepherdDatabase(context).use { db ->
            db.activate(2_001)
            db.observe(listOf(row()), 3_000, ZoneId.of("UTC"))
            assertTrue(db.calls().isEmpty())
        }
        context.deleteDatabase("shepherd.sqlite3")
        ShepherdDatabase(context).use { db ->
            db.activate(1_000)
            db.observe(listOf(row()), 3_000, ZoneId.of("UTC"))
            db.observe(listOf(row(200, 20)), 33_000, ZoneId.of("UTC"))
            assertTrue(db.calls().none { it.status == CallStatus.UNSHARED })
            db.observe(listOf(row(200, 20)), 63_000, ZoneId.of("UTC"))
            assertEquals(1, db.pendingCalls().size)
        }
    }
    @Test fun deletionAndEmptyMetadataBreakTheObservationWindow() {
        ShepherdDatabase(context).use { db ->
            db.activate(1_000)
            db.observe(listOf(row()), 3_000, ZoneId.of("UTC"))
            assertFalse(db.observe(emptyList(), 33_000, ZoneId.of("UTC")))
            assertTrue(db.calls().isEmpty())
            db.observe(listOf(row()), 60_000, ZoneId.of("UTC"))
            db.observe(listOf(row(0)), 90_000, ZoneId.of("UTC"))
            db.observe(listOf(row()), 120_000, ZoneId.of("UTC"))
            assertTrue(db.calls().none { it.status == CallStatus.UNSHARED })
            db.observe(listOf(row()), 150_000, ZoneId.of("UTC"))
            assertEquals(1, db.pendingCalls().size)
        }
    }
    @Test fun reindexPreservesUnsharedAndProcessedAndExcludesHistoricalFiles() {
        ShepherdDatabase(context).use { db ->
            db.activate(1_000)
            db.observe(listOf(row()), 3_000, ZoneId.of("UTC"))
            db.observe(listOf(row()), 33_000, ZoneId.of("UTC"))
            val reindexed = row().copy(uri = "content://media/external/audio/media/2", addedAt = 50_000)
            assertFalse(db.observe(listOf(reindexed), 50_000, ZoneId.of("Asia/Seoul")))
            assertEquals(reindexed.uri, db.pendingCalls().single().id)
            assertTrue(db.markProcessed(reindexed.uri))
            val again = reindexed.copy(uri = "content://media/external/audio/media/3", addedAt = 60_000)
            db.observe(listOf(again), 60_000, ZoneId.of("UTC"))
            assertEquals(CallStatus.PROCESSED, db.calls().single().status)
            assertEquals(again.uri, db.calls().single().id)
            val historical = row().copy(uri = "historical", addedAt = 70_000, modified = 0)
            db.observe(listOf(again, historical), 70_000, ZoneId.of("UTC"))
            db.observe(listOf(again, historical), 100_000, ZoneId.of("UTC"))
            assertEquals(1, db.calls().size)
            assertTrue(db.pendingCalls().isEmpty())
        }
    }
    @Test fun earlySharedTranscriptReservesProcessingAcrossGrowthAndRestart() {
        ShepherdDatabase(context).use { db ->
            db.activate(1_000)
            db.observe(listOf(row()), 3_000, ZoneId.of("Asia/Seoul"))
            val match = db.pendingCalls().single()
            assertEquals("친구", match.counterpart)
            assertTrue(db.markProcessed(match.id))
            assertFalse(db.markProcessed(match.id))
            assertTrue(db.calls().isEmpty())
            assertTrue(db.pendingCalls().isEmpty())
            db.observe(listOf(row(200, 20)), 33_000, ZoneId.of("UTC"))
        }
        ShepherdDatabase(context).use { db ->
            db.observe(listOf(row(200, 20)), 63_000, ZoneId.of("UTC"))
            assertEquals(CallStatus.PROCESSED, db.calls().single().status)
            assertFalse(db.markProcessed(row().uri))
        }
    }
    @Test fun deletedUnsharedIsRemovedAndReindexRestoresItButPreservesDismissal() {
        ShepherdDatabase(context).use { db ->
            db.activate(1_000)
            db.observe(listOf(row()), 3_000, ZoneId.of("UTC"))
            db.observe(listOf(row()), 33_000, ZoneId.of("UTC"))
            db.observe(emptyList(), 40_000, ZoneId.of("UTC"))
            assertEquals(CallStatus.REMOVED, db.calls().single().status)
            assertTrue(db.pendingCalls().isEmpty())
            val reindexed = row().copy(uri = "reindexed", addedAt = 50_000)
            db.observe(listOf(reindexed), 50_000, ZoneId.of("UTC"))
            assertEquals(CallStatus.UNSHARED, db.calls().single().status)
            assertTrue(db.dismiss(reindexed.uri))
            assertFalse(db.dismiss(reindexed.uri))
            assertFalse(db.markProcessed(reindexed.uri))
            assertTrue(db.pendingCalls().isEmpty())
            db.observe(emptyList(), 60_000, ZoneId.of("UTC"))
            db.observe(listOf(reindexed.copy(uri = "again")), 70_000, ZoneId.of("UTC"))
            assertEquals("again", db.calls().single().id)
            assertEquals(CallStatus.DISMISSED, db.calls().single().status)
            assertTrue(db.pendingCalls().isEmpty())
        }
    }
    @Test fun strictNamesAndInvalidDatesHaveNoHints() {
        val zone = ZoneId.of("Asia/Seoul")
        assertEquals("2026-11-08T12:34:56+09:00",
            CallFilename.parse("Call recording Person_20261108_123456.m4a", zone).occurredAt)
        listOf("Person_261132_123456.m4a", "Person_261108_253456.m4a", "Person_261108_123456.mp3",
            "Person_261108_123456 (1).m4a", "_261108_123456.m4a").forEach {
            assertEquals(CallHints(null, null), CallFilename.parse(it, zone))
        }
        assertEquals(CallHints(null, null), CallFilename.parse("Person_261101_013000.m4a", ZoneId.of("America/New_York")))
    }
    @Test fun pathsAreExactAndDoNotIncludeVoiceMemos() {
        assertTrue(RecordingPaths.matches("Recordings/Call/", null))
        assertFalse(RecordingPaths.matches("Recordings/Call/sub/", null))
        assertFalse(RecordingPaths.matches("Recordings/Voice/", null))
        assertTrue(RecordingPaths.matches(null, "/storage/emulated/0/Recordings/Call/a.m4a"))
        assertFalse(RecordingPaths.matches(null, "/storage/emulated/0/Recordings/Call/sub/a.m4a"))
        assertTrue(RecordingPaths.matches(null, "/storage/1234-ABCD/Recordings/Call/a.m4a"))
        listOf("/storage/1234-ABCD/sub/Recordings/Call/a.m4a",
            "/storage/random/Recordings/Call/a.m4a", "/storage/emulated/no/Recordings/Call/a.m4a",
            "/storage/emulated/0/sub/Recordings/Call/a.m4a").forEach {
            assertFalse(it, RecordingPaths.matches(null, it))
        }
    }
}
