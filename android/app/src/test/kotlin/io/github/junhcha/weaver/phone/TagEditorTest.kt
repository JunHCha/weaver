package io.github.junhcha.weaver.phone

import io.github.junhcha.weaver.transport.ApiException
import io.github.junhcha.weaver.transport.FakeMobileServer
import io.github.junhcha.weaver.transport.HttpsMobileApi
import java.time.Instant
import org.junit.After
import org.junit.Assert.*
import org.junit.Test

class TagEditorTest {
    private val server = FakeMobileServer()
    @After fun stop() = server.close()
    private fun editor() = TagEditor(HttpsMobileApi(FakeMobileServer.pairing(server), connectTimeoutMs = 500, readTimeoutMs = 1000))

    private fun thread(id: String, history: Boolean = false, kind: ThreadKind = ThreadKind.RECORD) = PhoneThread(
        id, 1, kind, "내용", Instant.EPOCH, Instant.EPOCH, "통화",
        presentation = if (history) Presentation.HISTORY else Presentation.FACT,
    )

    @Test fun editsOneThreadWithAddRemoveAndKinds() {
        val result = editor().editThread("thr_a", listOf("VPN"), listOf("네트워크"), mapOf("VPN" to TagKind.TOPIC))
        assertEquals(TagEditResult(1, 0), result)
        val (method, path, body) = server.writes.single()
        assertEquals("POST", method); assertEquals("/threads/thr_a/tags", path)
        assertTrue(body.contains("\"add\":[\"VPN\"]") && body.contains("\"remove\":[\"네트워크\"]") && body.contains("\"VPN\":\"topic\""))
    }

    @Test fun batchEditsManyThreadsInOneRequest() {
        val result = editor().editBatch(listOf("a", "b", "c"), listOf("VPN"), emptyList())
        assertEquals(3, result.changed)
        val (method, path, body) = server.writes.single()
        assertEquals("/threads/tags/batch", path)
        assertTrue(body.contains("\"thread_ids\":[\"a\",\"b\",\"c\"]"))
    }

    @Test fun aRepeatedActionWithTheSameKeyIsAppliedOnce() {
        val editor = editor()
        val key = editor.newKey()
        editor.editThread("thr_a", listOf("VPN"), emptyList(), key = key)
        editor.editThread("thr_a", listOf("VPN"), emptyList(), key = key) // response was lost; the sheet retries
        assertEquals(1, server.writes.size)
        assertEquals(2, server.writeKeys.size + 1)
    }

    @Test fun historyThreadsAreNotEditableOnThePhoneAndRefusedByTheServer() {
        assertFalse(thread("h", history = true, kind = ThreadKind.TAG_CORRECTION).tagEditable())
        assertTrue(thread("f").tagEditable())
        server.historyThreads.add("h")
        val error = assertThrows(ApiException::class.java) { editor().editThread("h", listOf("VPN"), emptyList()) }
        assertEquals(422, error.status); assertEquals("history_thread", error.code)
        assertTrue(server.writes.isEmpty())
    }

    @Test fun dictionaryEditsRenameKindAliasAndMerge() {
        val editor = editor()
        editor.rename("네트워크", "네트워크 인프라")
        editor.setKind("네트워크 인프라", TagKind.AREA)
        editor.addAlias("네트워크 인프라", "net")
        editor.removeAlias("네트워크 인프라", "net")
        editor.merge("VPN", "네트워크 인프라", TagKind.AREA)
        assertEquals(
            listOf("POST /dictionary/edit", "POST /dictionary/edit", "POST /dictionary/aliases", "POST /dictionary/aliases/remove", "POST /dictionary/merge"),
            server.writes.map { "${it.first} ${it.second}" },
        )
        assertTrue(server.writes[4].third.contains("\"kind\":\"area\""))
        assertEquals(5, server.writeKeys.toSet().size)
    }

    @Test fun readsTheDictionaryForSearchAndAliases() {
        server.dictionary = listOf("""{"name":"네트워크","kind":"topic","aliases":["net","망"],"usage_count":4}""")
        val entries = editor().dictionary()
        assertEquals(DictionaryEntry("네트워크", TagKind.TOPIC, listOf("net", "망"), 4), entries.single())
        val tags = listOf(PhoneTag("네트워크", TagKind.TOPIC), PhoneTag("VPN", null), PhoneTag("방화벽", null))
        assertEquals(listOf("네트워크"), suggestTags("net", tags, entries, emptySet()).map { it.name })
        assertEquals(listOf("VPN"), suggestTags("vp", tags, entries, setOf("네트워크")).map { it.name })
        assertTrue(suggestTags("  ", tags, entries, emptySet()).isEmpty())
        assertTrue(suggestTags("vpn", tags, entries, setOf("VPN")).isEmpty())
    }

    @Test fun anOfflineServerFailsTheEditWithoutChangingAnything() {
        server.offline = true
        assertThrows(io.github.junhcha.weaver.transport.UnreachableException::class.java) {
            editor().editThread("thr_a", listOf("VPN"), emptyList())
        }
        assertTrue(server.writes.isEmpty())
    }
}
