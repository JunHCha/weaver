package io.github.junhcha.weaver.transport

import io.github.junhcha.weaver.diagnostics.*
import java.nio.file.Files
import kotlinx.serialization.json.Json
import org.junit.Assert.*
import org.junit.Test

/** Synthetic exceptions only; the server generated fixtures validate every upload byte. */
class CrashDiagnosticsTest {
    private fun directory() = Files.createTempDirectory("weaver-crashes").toFile()
    private fun synthetic(): Throwable = IllegalStateException("PRIVATE SYNTHETIC MESSAGE").apply {
        stackTrace = arrayOf(StackTraceElement("io.github.junhcha.weaver.ui.Map", "open", "Map.kt", 42))
    }
    private fun screen() = CrashScreen("NOW", true, "USER")
    private fun fixtureStore(dir: java.io.File): CrashStore {
        val report = Json.decodeFromString<CrashReport>(ContractFixtures["crashes.stored"].requestBody.toString())
        return CrashStore(dir).also {
            val captured = it.capture(Thread("main"), synthetic(), screen(), report.client_id, report.occurred_at)
            assertEquals(report, captured)
        }
    }

    @Test fun handlerSavesBeforeDelegatingWithoutAnyThrowableMessages() {
        val dir = directory()
        val store = CrashStore(dir)
        var delegated = false
        val error = synthetic().apply {
            initCause(IllegalArgumentException("PRIVATE CAUSE"))
            addSuppressed(IllegalAccessException("PRIVATE SUPPRESSED"))
        }
        CrashHandler(store, ::screen) { _, original ->
            assertSame(error, original)
            assertEquals(1, store.pending().size)
            delegated = true
        }.uncaughtException(Thread("main"), error)
        assertTrue(delegated)
        val text = dir.listFiles()!!.single { it.extension == "json" }.readText()
        assertFalse(text.contains("PRIVATE"))
        assertFalse(text.contains("\"message\":", ignoreCase = true))
        val report = CrashStore(dir).pending().single()
        assertEquals(3, report.exceptions.size)
        assertEquals("io.github.junhcha.weaver.ui.Map", report.exceptions.first().frames.first().class_name)
        assertEquals(screen(), report.screen)
    }

    @Test fun retainsOnlyNewestFiveAndAlwaysDelegatesIfDiskFails() {
        val dir = directory()
        val store = CrashStore(dir)
        repeat(7) { store.capture(Thread("main"), synthetic(), screen(), "synthetic_$it", "2026-11-19T00:00:0${it}Z") }
        assertEquals(5, CrashStore(dir).pending().size)
        assertEquals("synthetic_6", store.pending().first().client_id)
        var delegated = false
        val file = java.io.File(dir, "not-a-directory").apply { writeText("synthetic") }
        CrashHandler(CrashStore(file), ::screen) { _, _ -> delegated = true }.uncaughtException(Thread("main"), synthetic())
        assertTrue(delegated)
    }

    @Test fun nextLaunchUploadsFixtureAndDeletesOnlyAfterStoredAcknowledgement() {
        val dir = directory()
        fixtureStore(dir)
        FakeMobileServer().use { server ->
            server.serve("crashes.stored")
            // A new instance simulates the next process: nothing is kept only in memory.
            CrashStore(dir).send(HttpsMobileApi(FakeMobileServer.pairing(server)))
            assertTrue(CrashStore(dir).pending().isEmpty())
            assertNotNull(CrashStore(dir).lastSummary())
            assertEquals(1, server.count("/diagnostics/crashes"))
            assertTrue(server.mismatches.toString(), server.mismatches.isEmpty())
        }
    }

    @Test fun lostResponseRetainsOriginalIdAndReplayDeletesIt() {
        val dir = directory()
        fixtureStore(dir)
        FakeMobileServer().use { server ->
            server.serve("crashes.stored", "crashes.replay")
            server.dropResponse += "/diagnostics/crashes"
            val api = HttpsMobileApi(FakeMobileServer.pairing(server))
            runCatching { CrashStore(dir).send(api) }
            // HttpURLConnection may retry a POST internally; if it does, the fixture checks the same ID and body.
            if (CrashStore(dir).pending().isNotEmpty()) CrashStore(dir).send(api)
            assertTrue(CrashStore(dir).pending().isEmpty())
            assertTrue(server.mismatches.toString(), server.mismatches.isEmpty())
        }
    }

    @Test fun refusalKeepsRecordForManualRetry() {
        val dir = directory()
        fixtureStore(dir)
        FakeMobileServer().use { server ->
            server.failWith("/diagnostics/crashes", "errors.whois_unavailable")
            assertTrue(runCatching { CrashStore(dir).send(HttpsMobileApi(FakeMobileServer.pairing(server))) }.isFailure)
            assertEquals(1, CrashStore(dir).pending().size)
            server.clearFailures()
            server.serve("crashes.stored")
            CrashStore(dir).send(HttpsMobileApi(FakeMobileServer.pairing(server)))
            assertTrue(CrashStore(dir).pending().isEmpty())
        }
    }
}
