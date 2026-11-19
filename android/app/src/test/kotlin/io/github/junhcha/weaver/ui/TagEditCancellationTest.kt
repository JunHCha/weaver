package io.github.junhcha.weaver.ui

import io.github.junhcha.weaver.transport.InvalidResponseException
import io.github.junhcha.weaver.transport.UnreachableException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test

class TagEditCancellationTest {
    @Test fun dictionaryLoadCancellationDoesNotBecomeAnEmptyDictionary() = runBlocking {
        val cancellation = CancellationException("dictionary cancelled")
        suspend fun dictionary(): List<String> = throw cancellation
        try {
            editAttempt { dictionary() }.getOrDefault(emptyList())
            fail("dictionary cancellation must escape")
        } catch (error: CancellationException) { assertSame(cancellation, error) }
    }

    @Test fun saveCancellationDoesNotBecomeAnEditErrorOrSuccess() = runBlocking {
        val cancellation = CancellationException("save cancelled")
        var completed = false
        var errorShown = false
        suspend fun save(): Unit = throw cancellation
        try {
            editAttempt { save() }.onSuccess { completed = true }.onFailure { errorShown = true }
            fail("save cancellation must escape")
        } catch (error: CancellationException) { assertSame(cancellation, error) }
        assertFalse(completed)
        assertFalse(errorShown)
    }

    @Test fun ordinaryFailuresStayReportableAndFormatFailuresAreNotOffline() {
        val error: Throwable = InvalidResponseException()
        assertFalse(error is UnreachableException)
        assertSame(error, editAttempt { throw error }.exceptionOrNull())
        assertEquals("서버 응답 형식이 올바르지 않아요", editErrorText(error))
        assertEquals("서버 응답 형식이 올바르지 않아요", pairingErrorText(error))
    }
}
