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
    private var tooLarge = false
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
            if (tooLarge) throw ApiException(200, "response_too_large", false)
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
        assertTrue(controller.acknowledge(controller.snapshot!!.head, controller.snapshot!!.epoch))
        assertEquals(snap.head, store.ack(server, snap.epoch))
        assertTrue(controller.visible()!!.recent.isEmpty())
        assertEquals(0L, store.ack("another-server", snap.epoch))
    }

    @Test fun onlineKeepsServerOrderAndOfflineSortsByOccurrenceThenSeq() = runBlocking {
        val template = raw.getValue("recent").jsonArray.first().jsonObject
        fun row(id: String, seq: Long, occurred: String, recorded: String) = JsonObject(template.toMutableMap().apply {
            put("thread", JsonObject(template.getValue("thread").jsonObject.toMutableMap().apply {
                put("thread_id", JsonPrimitive(id)); put("seq", JsonPrimitive(seq)); put("revision", JsonPrimitive(seq))
                put("occurred_at", JsonPrimitive(occurred)); put("recorded_at", JsonPrimitive(recorded))
                put("kind", JsonPrimitive("기록")); put("open", JsonPrimitive(false)); put("field", JsonNull); put("value", JsonNull)
                put("effective_deadline", JsonNull); put("effective_schedule", JsonNull)
            }))
        })
        change("head" to JsonPrimitive(4), "overdue" to JsonArray(emptyList()), "due_today" to JsonArray(emptyList()),
            "recent" to JsonArray(listOf(
                row("late", 3, "2026-11-06T00:00:00Z", "2026-11-20T00:00:00Z"),
                row("newer", 1, "2026-11-19T00:00:00Z", "2026-11-19T00:00:00Z"),
                row("tie", 2, "2026-11-19T00:00:00Z", "2026-11-19T00:00:00Z"),
                row("unseen-old", 4, "2026-11-05T00:00:00Z", "2026-11-05T00:00:00Z"),
            )))
        val controller = controller(); controller.refresh()
        assertEquals(listOf("late", "newer", "tie", "unseen-old"), controller.visible()!!.recent.map { it.thread.id })
        failRead = true
        val restarted = controller(); restarted.refresh()
        assertEquals(listOf("tie", "newer", "late", "unseen-old"), restarted.visible()!!.recent.map { it.thread.id })
        assertEquals(0L, store.ack(server, restarted.snapshot!!.epoch))
    }

    @Test fun requestRevisedAfterAckIsMarkedRevisedAndKeepsOccurrencePosition() = runBlocking {
        val template = raw.getValue("recent").jsonArray.first().jsonObject
        fun row(id: String, seq: Long, revision: Long, occurred: String) = JsonObject(template.toMutableMap().apply {
            put("thread", JsonObject(template.getValue("thread").jsonObject.toMutableMap().apply {
                put("thread_id", JsonPrimitive(id)); put("seq", JsonPrimitive(seq)); put("revision", JsonPrimitive(revision))
                put("occurred_at", JsonPrimitive(occurred)); put("recorded_at", JsonPrimitive(occurred))
                put("kind", JsonPrimitive("기록")); put("open", JsonPrimitive(false)); put("field", JsonNull); put("value", JsonNull)
                put("effective_deadline", JsonNull); put("effective_schedule", JsonNull)
            }))
        })
        fun recent(head: Long, vararg rows: JsonObject) = change("head" to JsonPrimitive(head), "overdue" to JsonArray(emptyList()),
            "due_today" to JsonArray(emptyList()), "recent" to JsonArray(rows.toList()))
        recent(2, row("old", 1, 1, "2026-11-06T00:00:00Z"))
        val controller = controller(); controller.refresh()
        assertTrue(controller.acknowledge(controller.snapshot!!.head, controller.snapshot!!.epoch))
        recent(6, row("fresh", 6, 6, "2026-11-19T00:00:00Z"), row("old", 1, 5, "2026-11-06T00:00:00Z"))
        controller.refresh()
        val visible = controller.visible()!!.recent
        assertEquals(listOf("fresh", "old"), visible.map { it.thread.id })
        assertEquals(listOf(false, true), visible.map { it.revised })
    }

    @Test fun malformedOrUnknownRowPreservesCacheAndCannotAcknowledge() = runBlocking {
        val controller = controller(); controller.refresh()
        val before = controller.snapshot
        editThread { JsonObject(it.toMutableMap().apply { put("kind", JsonPrimitive("future-kind")) }) }
        controller.refresh()
        assertFalse(controller.online); assertFalse(controller.acknowledge(controller.snapshot!!.head, controller.snapshot!!.epoch))
        assertSame(before, controller.snapshot)
        assertEquals(0L, store.ack(server, before!!.epoch))
        assertEquals(before, controller().also { it.refresh() }.snapshot)
    }

    @Test fun truncatedReadAndOfflineRestartDoNotAdvanceBookmark() = runBlocking {
        val controller = controller(); controller.refresh(); failRead = true; controller.refresh()
        assertFalse(controller.acknowledge(controller.snapshot!!.head, controller.snapshot!!.epoch))
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
        controller.refreshLocal()
        assertTrue(controller.visible()!!.recent.any { it.thread.id == thread.id })
        controller.hide(thread)
        assertTrue(controller.suppressed().any { it.id == thread.id })
        assertFalse(store.suppressed("other", controller.snapshot!!.epoch, thread, clock))
        assertFalse(store.suppressed(server, "another-epoch", thread, clock))
        assertFalse(store.suppressed(server, controller.snapshot!!.epoch, thread.copy(revision = thread.revision + 1), clock))
        controller.refreshLocal()
        assertTrue(controller.visible()!!.recent.any { it.thread.id == thread.id })
    }

    @Test fun hiddenItemsRemainRestorableAfterTheyLeaveAcknowledgedSnapshot() = runBlocking {
        val controller = controller(); controller.refresh(); val thread = controller.snapshot!!.threads.first()
        controller.hide(thread); controller.acknowledge(controller.snapshot!!.head, controller.snapshot!!.epoch)
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
        controller.undo(store.operations(server).single())
        assertEquals(setOf("client_id", "edit_thread_id", "expected_revision"), sent.last().second.keys)
        assertEquals(store.operations(server).first().result!!.getValue("edit_thread_id"), sent.last().second.getValue("edit_thread_id"))
        assertEquals(store.operations(server).first().result!!.getValue("revision"), sent.last().second.getValue("expected_revision"))
        assertNotEquals(sent.first().second["client_id"], sent.last().second["client_id"])
    }

    @Test fun conflictIsTerminalAndNeverSilentlyRetried() = runBlocking {
        val controller = controller(); controller.refresh(); val thread = controller.snapshot!!.threads.first()
        editError = ApiException(409, "revision_conflict", false)
        controller.submit(thread, "complete", "완료를 확인했습니다")
        assertEquals("rejected", store.operations(server).single().state)
        assertEquals("revision_conflict", store.operations(server).single().code)
        assertEquals("revision_conflict", controller.uiState.value.problem)
        controller.refresh(); assertEquals(1, sent.size)
    }

    @Test fun epochMismatchReadsNewGenerationAtZeroAndQuarantinesPending() = runBlocking {
        val controller = controller(); controller.refresh(); val thread = controller.snapshot!!.threads.first()
        controller.acknowledge(controller.snapshot!!.head, controller.snapshot!!.epoch)
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
        controller.acknowledge(controller.snapshot!!.head, controller.snapshot!!.epoch)
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
        assertFalse(controller.online); assertFalse(controller.acknowledge(controller.snapshot!!.head, controller.snapshot!!.epoch))
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

    @Test fun renderedBookmarkCannotAcknowledgeANewerHeadOrAnotherEpoch() = runBlocking {
        val controller = controller(); controller.refresh()
        val rendered = controller.snapshot!!
        change("head" to JsonPrimitive(rendered.head + 1))
        controller.refresh()
        assertFalse(controller.acknowledge(rendered.head, rendered.epoch))
        assertEquals(0L, store.ack(server, rendered.epoch))
        change("fabric_epoch" to JsonPrimitive("new-epoch"))
        readError = ApiException(409, "epoch_mismatch", false)
        controller.refresh()
        assertFalse(controller.acknowledge(controller.snapshot!!.head, rendered.epoch))
        assertEquals(0L, store.ack(server, "new-epoch"))
    }

    @Test fun outOfRangeBookmarkResetsWithinTheSameEpoch() = runBlocking {
        val controller = controller(); controller.refresh()
        val rendered = controller.snapshot!!
        controller.acknowledge(rendered.head, rendered.epoch)
        readError = ApiException(409, "checkpoint_out_of_range", false)
        controller.refresh()
        assertEquals(0L to rendered.epoch, reads.last())
        assertEquals(0L, store.ack(server, rendered.epoch))
        assertTrue(controller.online)
    }

    @Test fun forbiddenWriteKeepsPendingAndReusesItsBodyAfterTokenRepair() = runBlocking {
        val controller = controller(); controller.refresh()
        editError = ApiException(403, "scope_required", false)
        controller.submit(controller.snapshot!!.threads.first(), "complete", "완료했습니다")
        assertEquals("pending", store.operations(server).single().state)
        editError = null
        controller.refresh()
        assertEquals(sent.first(), sent.last())
        assertEquals("applied", store.operations(server).single().state)
    }

    @Test fun expiredSnoozeReturnsFalseWithoutWritingOrRefetching() = runBlocking {
        val controller = controller(); controller.refresh()
        val renderedTime = clock.plusSeconds(1)
        clock = clock.plusSeconds(2)
        val before = reads.size
        assertFalse(controller.snooze(controller.snapshot!!.threads.first(), renderedTime))
        assertTrue(controller.suppressed().isEmpty())
        assertEquals(before, reads.size)
        assertTrue(controller.online)
    }

    @Test fun localWritesPublishStateWithoutAnyNetworkRead() = runBlocking {
        val controller = controller(); controller.refresh(); val thread = controller.snapshot!!.threads.first()
        val before = reads.size
        assertTrue(controller.hide(thread)); assertEquals(listOf(thread), controller.uiState.value.suppressed)
        assertTrue(controller.restore(thread)); assertTrue(controller.uiState.value.suppressed.isEmpty())
        assertTrue(controller.snooze(thread, clock.plusSeconds(60)))
        assertTrue(controller.acknowledge(controller.snapshot!!.head, controller.snapshot!!.epoch))
        assertEquals(before, reads.size)
        assertTrue(controller.uiState.value.online)
    }

    @Test fun damagedViewRowIsRemovedWithoutLosingOtherRows() = runBlocking {
        val controller = controller(); controller.refresh(); val thread = controller.snapshot!!.threads.first()
        store.setView(server, controller.snapshot!!.epoch, thread, hidden = true)
        val valid = thread.copy(id = "valid-other-row")
        store.setView(server, controller.snapshot!!.epoch, valid, hidden = true)
        store.writableDatabase.execSQL("UPDATE view_state SET payload='{' WHERE thread=?", arrayOf(thread.id))
        controller.refreshLocal()
        assertEquals(listOf(valid), controller.suppressed())
        assertEquals(listOf(valid), store.savedViews(server, controller.snapshot!!.epoch))
    }

    @Test fun failedLocalDatabaseWriteReturnsFalseAndPublishesGuidance() = runBlocking {
        val controller = controller(); controller.refresh()
        store.writableDatabase.execSQL("DROP TABLE view_state")
        assertFalse(controller.hide(controller.snapshot!!.threads.first()))
        assertFalse(controller.restore(controller.snapshot!!.threads.first()))
        assertFalse(controller.snooze(controller.snapshot!!.threads.first(), clock.plusSeconds(60)))
        assertEquals("local_storage_error", controller.uiState.value.problem)
    }

    @Test fun immutableUiReadsDoNotAccessTheDatabase() = runBlocking {
        val controller = controller(); controller.refresh()
        val state = controller.uiState.value
        store.writableDatabase.execSQL("DROP TABLE view_state")
        store.writableDatabase.execSQL("DROP TABLE pending")
        store.writableDatabase.execSQL("DROP TABLE bookmark")
        assertEquals(state.visible, controller.visible())
        assertEquals(state.suppressed, controller.suppressed())
        assertEquals(state.operations, controller.operations())
        assertEquals(state.nextRefreshAt, controller.nextRefresh(clock))
    }

    @Test fun databaseOpenAndLocalWritesAreDispatchedOffMain() = runBlocking {
        val main = android.os.Looper.getMainLooper().thread
        val opened = java.util.concurrent.atomic.AtomicInteger()
        val guarded = object : NowStore(ApplicationProvider.getApplicationContext(), "guarded-${UUID.randomUUID()}.db") {
            override fun onOpen(db: android.database.sqlite.SQLiteDatabase) {
                assertNotEquals(main, Thread.currentThread())
                opened.incrementAndGet()
                super.onOpen(db)
            }
        }
        guarded.use {
            val controller = NowController(server, { api }, guarded) { clock }
            assertEquals(0, opened.get())
            controller.refresh()
            assertEquals(1, opened.get())
            guarded.close() // force a reopen on the next local write
            assertTrue(controller.hide(controller.snapshot!!.threads.first()))
            assertEquals(2, opened.get())
        }
    }

    @Test fun oversizedUnpagedResponseShowsProblemAndStopsPeriodicRetry() = runBlocking {
        tooLarge = true
        val controller = controller(); controller.refresh()
        assertEquals("response_too_large", controller.uiState.value.problem)
        assertNull(controller.uiState.value.nextRefreshAt)
        assertFalse(controller.online)
    }

    @Test fun migrationsRefuseToDropAPendingJournal() = runBlocking {
        val controller = controller(); controller.refresh()
        editError = java.io.IOException("lost")
        controller.submit(controller.snapshot!!.threads.first(), "complete", "완료")
        val before = store.operations(server)
        assertThrows(android.database.sqlite.SQLiteException::class.java) { store.onUpgrade(store.writableDatabase, 1, 2) }
        assertThrows(android.database.sqlite.SQLiteException::class.java) { store.onDowngrade(store.writableDatabase, 2, 1) }
        assertEquals(before, store.operations(server))
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
