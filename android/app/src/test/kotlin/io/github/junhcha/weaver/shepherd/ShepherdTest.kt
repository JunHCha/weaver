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
            assertTrue(db.pendingCalls().isEmpty())
        }
        ShepherdDatabase(context).use { db ->
            assertTrue(db.observe(listOf(row()), 32_999, ZoneId.of("UTC")))
            assertTrue(db.pendingCalls().isEmpty())
            assertFalse(db.observe(listOf(row()), 33_000, ZoneId.of("UTC")))
            val call = db.pendingCalls().single()
            assertEquals("친구", call.counterpart)
            assertEquals("2026-11-08T12:34:56+09:00", call.occurredAt)
            assertTrue(db.markProcessed(call.id))
            assertFalse(db.markProcessed(call.id))
            db.observe(listOf(row()), 100_000, ZoneId.of("UTC"))
            assertTrue(db.pendingCalls().isEmpty())
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
            assertTrue(db.pendingCalls().isEmpty())
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
            assertTrue(db.pendingCalls().isEmpty())
            db.observe(listOf(row()), 150_000, ZoneId.of("UTC"))
            assertEquals(1, db.pendingCalls().size)
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
    }
}
