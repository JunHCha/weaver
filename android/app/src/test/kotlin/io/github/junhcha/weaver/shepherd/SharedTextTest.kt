package io.github.junhcha.weaver.shepherd

import org.junit.Assert.*
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.File

class SharedTextTest {
    private fun copy(bytes: ByteArray): ByteArray {
        val file = File.createTempFile("share", ".txt")
        return try {
            SharedText.copy(ByteArrayInputStream(bytes), file)
            file.readBytes()
        } finally { file.delete() }
    }
    @Test fun preservesUtf8BomAndAllOriginalBytes() {
        val bytes = "\uFEFF안녕하세요\r\ntext".toByteArray()
        assertArrayEquals(bytes, copy(bytes))
    }
    @Test fun rejectsEmptyInvalidAndOtherEncodingsAndCleansUp() {
        listOf(byteArrayOf(), " \n\uFEFF".toByteArray(), byteArrayOf(0xc3.toByte()),
            "한국어".toByteArray(charset("MS949")), "hello".toByteArray(Charsets.UTF_16),
            "hello".toByteArray(Charsets.UTF_16LE)).forEach { bytes ->
            val file = File.createTempFile("share", ".txt")
            try {
                assertThrows(Exception::class.java) { SharedText.copy(ByteArrayInputStream(bytes), file) }
                assertFalse(file.exists())
            } finally { file.delete() }
        }
    }
    @Test fun acceptsLimitAndRejectsOneByteOverWithCleanup() {
        assertEquals(SharedText.MAX_BYTES.toInt(), copy(ByteArray(SharedText.MAX_BYTES.toInt()) { 65 }).size)
        val file = File.createTempFile("share", ".txt")
        try {
            assertThrows(IllegalArgumentException::class.java) {
                SharedText.copy(ByteArrayInputStream(ByteArray(SharedText.MAX_BYTES.toInt() + 1) { 65 }), file)
            }
            assertFalse(file.exists())
        } finally { file.delete() }
    }
}
