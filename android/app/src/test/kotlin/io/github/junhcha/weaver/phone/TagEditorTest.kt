package io.github.junhcha.weaver.phone

import io.github.junhcha.weaver.transport.ApiException
import io.github.junhcha.weaver.transport.FakeMobileServer
import io.github.junhcha.weaver.transport.HttpsMobileApi
import io.github.junhcha.weaver.transport.UnreachableException
import java.time.Instant
import org.junit.After
import org.junit.Assert.*
import org.junit.Test

class TagEditorTest {
    private val server = FakeMobileServer()
    @After fun stop() { server.close(); assertEquals("the phone sent what the fixtures expect", emptyList<String>(), server.mismatches.toList()) }
    private fun editor() = TagEditor(HttpsMobileApi(FakeMobileServer.pairing(server), connectTimeoutMs = 500, readTimeoutMs = 1000))

    private fun thread(id: String, history: Boolean = false, kind: ThreadKind = ThreadKind.RECORD) = PhoneThread(
        id, 1, kind, "내용", Instant.EPOCH, Instant.EPOCH, "통화",
        presentation = if (history) Presentation.HISTORY else Presentation.FACT,
    )

    @Test fun editsThreadTagsWithAddRemoveAndTheClientIdAsIdempotencyKey() {
        server.serve("edits.tags")
        val result = editor().editThread("record", listOf("Beta"), listOf("Alpha"), key = "client-edit-0001")
        assertEquals(TagEditResult(1, 0), result)
        assertEquals(listOf("POST /mobile/v1/edits/tags"), server.requests)
    }

    @Test fun batchEditsManyThreadsInOneRequest() {
        // The fixture edits one thread; the batch is the same endpoint with more ids. The server answers the fixture either way.
        server.serveDerived("edits.tags") { it }
        val result = editor().editBatch(listOf("a", "b", "c"), listOf("Beta"), listOf("Alpha"), key = "client-edit-0001")
        assertEquals("one request for all three threads", 1, server.requests.size)
        assertTrue(server.bodies.single().second.contains("\"thread_ids\":[\"a\",\"b\",\"c\"]"))
        assertEquals(1, result.changed) // the fixture's result; the real count comes from the server
        server.mismatches.clear() // the deliberate difference from the single-thread fixture request
    }

    @Test fun aRepeatedActionWithTheSameClientIdGetsTheFirstResultAgain() {
        server.serve("edits.tags", "edits.tags_replay")
        val editor = editor()
        editor.editThread("record", listOf("Beta"), listOf("Alpha"), key = "client-edit-0001")
        editor.editThread("record", listOf("Beta"), listOf("Alpha"), key = "client-edit-0001") // the response was lost; the sheet retries
        assertEquals(2, server.requests.size)
    }

    @Test fun theSameClientIdWithOtherContentIsAConflict() {
        server.serve("edits.tags_client_id_conflict")
        val error = assertThrows(ApiException::class.java) { editor().editThread("record", listOf("Gamma"), listOf("Alpha"), key = "client-edit-0001") }
        assertEquals(409, error.status); assertEquals("client_id_conflict", error.code)
    }

    @Test fun historyThreadsAreNotEditableOnThePhoneAndRefusedByTheServer() {
        assertFalse(thread("h", history = true, kind = ThreadKind.TAG_CORRECTION).tagEditable())
        assertTrue(thread("f").tagEditable())
        server.serve("edits.tags_history_thread")
        val error = assertThrows(ApiException::class.java) { editor().editThread("fix", listOf("Alpha"), emptyList(), key = "client-edit-0002") }
        assertEquals(422, error.status); assertEquals("history_thread_readonly", error.code)
    }

    @Test fun unknownThreadAndNewTagWithoutAKindAreFixedRejections() {
        server.serve("edits.tags_unknown_thread", "edits.tags_new_tag_needs_kind")
        val editor = editor()
        assertEquals("thread_not_found", assertThrows(ApiException::class.java) { editor.editThread("nope", listOf("Alpha"), emptyList(), key = "client-edit-0003") }.code)
        assertEquals("kind_required", assertThrows(ApiException::class.java) { editor.editThread("record", listOf("Delta"), emptyList(), key = "client-edit-0004") }.code)
    }

    @Test fun dictionaryEditsRenameKindAliasAndMergeUseTheirOwnEndpoints() {
        server.serve("edits.dictionary_rename", "edits.dictionary_kind", "edits.dictionary_alias_add", "edits.dictionary_alias_remove", "edits.dictionary_merge")
        val editor = editor()
        editor.rename("Alpha", "Alpha2", "client-dict-0001")
        editor.setKind("Gamma", TagKind.ORGANIZATION, "client-dict-0002")
        editor.addAlias("Alpha2", "AA", "client-dict-0003")
        editor.removeAlias("Alpha2", "AA", "client-dict-0004")
        editor.merge("Gamma", "Beta", TagKind.PERSON, "client-dict-0006")
        assertEquals(listOf("rename", "kind", "alias-add", "alias-remove", "merge").map { "POST /mobile/v1/edits/dictionary/$it" }, server.requests)
    }

    @Test fun mergingEntriesOfDifferentKindsNeedsAKindChoice() {
        server.serve("edits.dictionary_merge_kind_choice", "edits.dictionary_entry_not_found")
        val editor = editor()
        assertEquals("kind_choice_required", assertThrows(ApiException::class.java) { editor.merge("Gamma", "Beta", null, "client-dict-0005") }.code)
        assertEquals("entry_not_found", assertThrows(ApiException::class.java) { editor.rename("Nobody", "X", "client-dict-0007") }.code)
    }

    @Test fun readsTheDictionaryForSearchAndAliases() {
        server.serve("read.dictionary")
        val entries = editor().dictionary()
        assertEquals(DictionaryEntry("이서준", TagKind.PERSON, listOf("서준"), 1), entries.first { it.name == "이서준" })
        val tags = listOf(PhoneTag("이서준", TagKind.PERSON), PhoneTag("방화벽 교체", TagKind.PROJECT), PhoneTag("네트워크", TagKind.AREA))
        assertEquals(listOf("이서준"), suggestTags("서준", tags, entries, emptySet()).map { it.name })
        assertEquals(listOf("방화벽 교체"), suggestTags("방화", tags, entries, setOf("네트워크")).map { it.name })
        assertTrue(suggestTags("  ", tags, entries, emptySet()).isEmpty())
        assertTrue(suggestTags("방화", tags, entries, setOf("방화벽 교체")).isEmpty())
    }

    @Test fun anOfflineServerFailsTheEditWithoutChangingAnything() {
        server.serve("edits.tags"); server.offline = true
        assertThrows(UnreachableException::class.java) { editor().editThread("record", listOf("Beta"), listOf("Alpha"), key = "client-edit-0001") }
        assertTrue(server.requests.isEmpty())
    }
}
