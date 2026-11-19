package io.github.junhcha.weaver.phone

import androidx.test.core.app.ApplicationProvider
import io.github.junhcha.weaver.transport.ApiException
import io.github.junhcha.weaver.transport.ContractFixtures
import io.github.junhcha.weaver.transport.MobileApi
import java.lang.reflect.Proxy
import java.time.Instant
import java.time.LocalDate
import java.util.UUID
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.*
import org.junit.After
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class NowControllerTest {
    private val store = NowStore(ApplicationProvider.getApplicationContext(), "now-${UUID.randomUUID()}.db")
    private val server = "server-one"
    private var clock = Instant.parse("2026-11-19T00:00:00Z")
    private var supported = true
    private var scope = true
    private var raw = ContractFixtures["request_handling.now_since_seq"].responseBody!!.jsonObject
    private var failRead = false
    private var editError: Exception? = null
    private var readError: ApiException? = null
    private val reads = mutableListOf<Pair<Long?, String?>>()
    private val sent = mutableListOf<Pair<String, JsonObject>>()
    private val unused = Proxy.newProxyInstance(MobileApi::class.java.classLoader, arrayOf(MobileApi::class.java)) { _, method, _ -> error("unexpected ${method.name}") } as MobileApi
    private val api = object : MobileApi by unused {
        override fun capabilities() = buildJsonObject {
            put("server_id", server)
            put("features", buildJsonObject { put("thread_handling", supported) })
            put("device", buildJsonObject { put("scopes", JsonArray(if (scope) listOf(JsonPrimitive("edit")) else emptyList())) })
        }
        override fun now(afterSeq: Long?, fabricEpoch: String?): JsonObject {
            reads += afterSeq to fabricEpoch
            if (failRead) throw java.io.IOException("offline")
            if (afterSeq != null && readError != null) { val error = readError!!; readError = null; throw error }
            return raw
        }
        override fun edit(path: String, body: JsonObject): JsonObject {
            sent += path to body
            editError?.let { throw it }
            return ContractFixtures["request_handling.${path.substringAfterLast('/')}"].responseBody!!.jsonObject
        }
    }
    private fun controller(serverId: String = server) = NowController(serverId, { api }, store) { clock }
    private fun change(vararg fields: Pair<String, JsonElement>) { raw = JsonObject(raw.toMutableMap().apply { putAll(fields) }) }
    private fun editThread(transform: (JsonObject) -> JsonObject) {
        change("recent" to JsonArray(raw.getValue("recent").jsonArray.mapIndexed { index, item ->
            if (index != 0) item else JsonObject(item.jsonObject.toMutableMap().apply { put("thread", transform(item.jsonObject.getValue("thread").jsonObject)) })
        }))
    }
    @After fun close() { store.close() }

    @Test fun firstFetchStartsAtZeroAndOnlyExplicitAckAdvances() = runBlocking {
        val controller = controller(); controller.refresh()
        val snap = controller.snapshot!!
        assertEquals(listOf(null to null, 0L to snap.epoch), reads)
        assertEquals(0L, store.ack(server, snap.epoch))
        assertTrue(controller.acknowledge())
        assertEquals(snap.head, store.ack(server, snap.epoch))
        assertTrue(controller.visible()!!.recent.isEmpty())
        assertEquals(0L, store.ack("another-server", snap.epoch))
    }

    @Test fun malformedOrUnknownRowPreservesCacheAndCannotAcknowledge() = runBlocking {
        val controller = controller(); controller.refresh()
        val before = controller.snapshot
        editThread { JsonObject(it.toMutableMap().apply { put("kind", JsonPrimitive("future-kind")) }) }
        controller.refresh()
        assertFalse(controller.online); assertFalse(controller.acknowledge())
        assertSame(before, controller.snapshot)
        assertEquals(0L, store.ack(server, before!!.epoch))
        assertEquals(before, controller().also { it.refresh() }.snapshot)
    }

    @Test fun truncatedReadAndOfflineRestartDoNotAdvanceBookmark() = runBlocking {
        val controller = controller(); controller.refresh(); failRead = true; controller.refresh()
        assertFalse(controller.acknowledge())
        val restarted = controller(); restarted.refresh()
        assertFalse(restarted.online); assertTrue(restarted.supported)
        assertEquals(controller.snapshot, restarted.snapshot)
        assertEquals(0L, store.ack(server, restarted.snapshot!!.epoch))
    }

    @Test fun snoozeExpiresAndChangedRevisionUnhidesAndServerEpochIsolates() = runBlocking {
        val controller = controller(); controller.refresh()
        val thread = controller.snapshot!!.threads.first()
        controller.snooze(thread, clock.plusSeconds(60))
        assertFalse(controller.visible()!!.recent.any { it.thread.id == thread.id })
        clock = clock.plusSeconds(61)
        assertTrue(controller.visible()!!.recent.any { it.thread.id == thread.id })
        controller.hide(thread)
        assertTrue(controller.suppressed().any { it.id == thread.id })
        assertFalse(store.suppressed("other", controller.snapshot!!.epoch, thread, clock))
        assertFalse(store.suppressed(server, "another-epoch", thread, clock))
        assertFalse(store.suppressed(server, controller.snapshot!!.epoch, thread.copy(revision = thread.revision + 1), clock))
        assertTrue(controller.visible()!!.recent.any { it.thread.id == thread.id })
    }

    @Test fun hiddenItemsRemainRestorableAfterTheyLeaveAcknowledgedSnapshot() = runBlocking {
        val controller = controller(); controller.refresh(); val thread = controller.snapshot!!.threads.first()
        controller.hide(thread); controller.acknowledge()
        change("recent" to JsonArray(emptyList()))
        controller.refresh()
        assertEquals(thread, controller.suppressed().first())
        controller.restore(thread)
        assertTrue(controller.suppressed().isEmpty())
    }

    @Test fun tomorrowUsesServerTimezone() = runBlocking {
        change("timezone" to JsonPrimitive("America/Los_Angeles"))
        val controller = controller(); controller.refresh()
        assertEquals(Instant.parse("2026-11-19T17:00:00Z"), controller.tomorrow())
    }

    @Test fun lostResponseReplaysDurableKeyAndExactBodyAfterRestart() = runBlocking {
        val controller = controller(); controller.refresh(); val thread = controller.snapshot!!.threads.first()
        editError = java.io.IOException("response_lost")
        controller.submit(thread, "complete", "이 요청을 완료했습니다")
        assertEquals("pending", store.operations(server).single().state)
        val body = sent.single().second
        assertEquals(setOf("client_id", "thread_id", "expected_revision", "content"), body.keys)
        assertEquals(thread.revision, body.getValue("expected_revision").jsonPrimitive.long)
        editError = null
        controller().refresh()
        assertEquals(sent.first(), sent.last())
        assertEquals("applied", store.operations(server).single().state)
        assertEquals(body, store.operations(server).single().body)
    }

    @Test fun deadlineAndUndoUseTheirOwnContractFields() = runBlocking {
        val controller = controller(); controller.refresh(); val thread = controller.snapshot!!.threads.first()
        controller.submit(thread, "deadline", "기한을 변경했습니다", LocalDate.parse("2026-12-01"))
        assertEquals("2026-12-01", sent.last().second.getValue("deadline").jsonPrimitive.content)
        controller.submit(thread, "undo", editThreadId = "phone-request:made-here")
        assertEquals(setOf("client_id", "edit_thread_id", "expected_revision"), sent.last().second.keys)
        assertEquals("phone-request:made-here", sent.last().second.getValue("edit_thread_id").jsonPrimitive.content)
        assertNotEquals(sent.first().second["client_id"], sent.last().second["client_id"])
    }

    @Test fun conflictIsTerminalAndNeverSilentlyRetried() = runBlocking {
        val controller = controller(); controller.refresh(); val thread = controller.snapshot!!.threads.first()
        editError = ApiException(409, "revision_conflict", false)
        controller.submit(thread, "complete", "완료를 확인했습니다")
        assertEquals("rejected", store.operations(server).single().state)
        assertEquals("revision_conflict", store.operations(server).single().code)
        controller.refresh(); assertEquals(1, sent.size)
    }

    @Test fun epochMismatchReadsNewGenerationAtZeroAndQuarantinesPending() = runBlocking {
        val controller = controller(); controller.refresh(); val thread = controller.snapshot!!.threads.first()
        controller.acknowledge()
        editError = java.io.IOException("response_lost")
        controller.submit(thread, "complete", "완료했습니다")
        assertEquals(1, sent.size)
        val oldEpoch = controller.snapshot!!.epoch
        change("fabric_epoch" to JsonPrimitive("new-fabric"))
        readError = ApiException(409, "epoch_mismatch", false)
        editError = null
        controller.refresh()
        assertEquals(0L to "new-fabric", reads.last())
        assertEquals("quarantined", store.operations(server).single().state)
        assertEquals(1, sent.size)
        assertEquals(0L, store.ack(server, "new-fabric"))
        assertEquals(4L, store.ack(server, oldEpoch))
    }

    @Test fun ackUsesDisplayedSnapshotHeadAndDueItemsRemain() = runBlocking {
        val entry = raw.getValue("recent").jsonArray.first()
        change("overdue" to JsonArray(listOf(entry)), "recent" to JsonArray(emptyList()))
        val controller = controller(); controller.refresh()
        val displayed = controller.snapshot!!.head
        change("head" to JsonPrimitive(displayed + 10)) // appended after the phone fetched
        controller.acknowledge()
        assertEquals(displayed, store.ack(server, controller.snapshot!!.epoch))
        assertEquals(1, controller.visible()!!.overdue.size)
    }

    @Test fun closureAndWithdrawalArePreservedAndRevisionChangeClearsHiddenState() = runBlocking {
        val controller = controller(); controller.refresh(); val original = controller.snapshot!!.threads.first()
        controller.hide(original)
        change("head" to JsonPrimitive(99))
        editThread { JsonObject(it.toMutableMap().apply {
            put("revision", JsonPrimitive(99)); put("open", JsonPrimitive(false)); put("retracted", JsonPrimitive(true))
        }) }
        controller.refresh()
        val closed = controller.snapshot!!.threads.first()
        assertEquals(false, closed.open); assertEquals(true, closed.retracted)
        assertTrue(controller.suppressed().isEmpty())
        assertTrue(controller.visible()!!.recent.any { it.thread.id == original.id })
        val cached = NowSnapshot.parse(store.snapshot(server)!!).threads.first()
        assertEquals(closed, cached)
    }

    @Test fun offlineMidnightReclassifiesCachedFactsInServerZoneAndSchedulesExpiry() = runBlocking {
        clock = Instant.parse("2026-11-19T23:59:00Z")
        val entry = raw.getValue("recent").jsonArray.first().jsonObject
        val thread = entry.getValue("thread").jsonObject
        val due = JsonObject(entry.toMutableMap().apply { put("thread", JsonObject(thread.toMutableMap().apply {
            put("effective_deadline", JsonPrimitive("2026-11-19")); put("open", JsonPrimitive(true)); put("retracted", JsonPrimitive(false))
        })); put("due_kind", JsonPrimitive("deadline")) })
        change("timezone" to JsonPrimitive("UTC"), "due_today" to JsonArray(listOf(due)), "recent" to JsonArray(emptyList()))
        val controller = controller(); controller.refresh()
        assertEquals(Instant.parse("2026-11-20T00:00:00Z"), controller.nextRefresh(clock))
        controller.snooze(controller.snapshot!!.threads.first(), clock.plusSeconds(30))
        assertEquals(clock.plusSeconds(30), controller.nextRefresh(clock))
        clock = clock.plusSeconds(61); failRead = true; controller.refresh()
        assertEquals(1, controller.visible()!!.overdue.size)
        assertTrue(controller.visible()!!.today.isEmpty())
    }

    @Test fun invalidDeadlineCannotPartiallyParseAndAdvanceAck() = runBlocking {
        val controller = controller(); controller.refresh()
        editThread { JsonObject(it.toMutableMap().apply { put("effective_deadline", JsonPrimitive("2026-99-99")) }) }
        controller.refresh()
        assertFalse(controller.online); assertFalse(controller.acknowledge())
        assertEquals(0L, store.ack(server, controller.snapshot!!.epoch))
    }

    @Test fun pendingBodySurvivesDatabaseReopenAndDuplicateKeyCannotReplaceIt() = runBlocking {
        val controller = controller(); controller.refresh()
        editError = java.io.IOException("lost")
        controller.submit(controller.snapshot!!.threads.first(), "complete", "완료했습니다")
        val pending = store.operations(server).single()
        val name = store.databaseName
        store.close()
        NowStore(ApplicationProvider.getApplicationContext(), name).use { reopened ->
            assertEquals(pending, reopened.operations(server).single())
            assertTrue(runCatching { reopened.enqueue(server, pending.epoch, pending.clientId, pending.path,
                JsonObject(pending.body.toMutableMap().apply { put("content", JsonPrimitive("different")) })) }.isFailure)
            assertEquals(pending.body, reopened.operations(server).single().body)
        }
    }

    @Test fun unsupportedServerFallsBackWithoutCallingNowAndReadOnlyScopeCannotWrite() = runBlocking {
        supported = false
        val controller = controller(); controller.refresh()
        assertFalse(controller.supported); assertNull(controller.snapshot); assertTrue(reads.isEmpty())
        supported = true; scope = false; controller.refresh()
        assertTrue(controller.supported); assertFalse(controller.canEdit)
        assertTrue(runCatching { controller.submit(controller.snapshot!!.threads.first(), "complete", "완료") }.isFailure)
        assertTrue(sent.isEmpty())
    }
}
