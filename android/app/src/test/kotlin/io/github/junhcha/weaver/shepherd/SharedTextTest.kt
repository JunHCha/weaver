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
            "한국어".toByteArray(charset("MS949")), "hello".toByteArray(Charsets.UTF_16BE),
            "hello".toByteArray(Charsets.UTF_16LE)).forEach { bytes ->
            val file = File.createTempFile("share", ".txt")
            try {
                assertThrows(Exception::class.java) { SharedText.copy(ByteArrayInputStream(bytes), file) }
                assertFalse(file.exists())
            } finally { file.delete() }
        }
    }
    @Test fun convertsBothUtf16BomsLosslesslyWithoutAddingUtf8Bom() {
        val text = "AI로 생성된 콘텐츠입니다\n\n가짜 제목\n\n나  (00:01)\n합성 대화 😀 \n"
        listOf(Charsets.UTF_16BE to byteArrayOf(0xfe.toByte(), 0xff.toByte()),
            Charsets.UTF_16LE to byteArrayOf(0xff.toByte(), 0xfe.toByte())).forEach { (charset, bom) ->
            val result = copy(bom + text.toByteArray(charset))
            assertArrayEquals(text.toByteArray(Charsets.UTF_8), result)
            assertEquals(text, result.toString(Charsets.UTF_8))
        }
    }
    @Test fun preservesCrLfWhitespaceAndContentBomBeyondEncodingBom() {
        val text = "\uFEFF가짜 내용 \r\n\r\n끝 \n"
        assertArrayEquals(text.toByteArray(), copy(byteArrayOf(0xff.toByte(), 0xfe.toByte()) +
            text.toByteArray(Charsets.UTF_16LE)))
    }
    @Test fun acceptsUtf16WhoseUtf8OutputIsExactlyLimit() {
        val text = "가".repeat(SharedText.MAX_BYTES.toInt() / 3) + "a"
        assertEquals(SharedText.MAX_BYTES.toInt(), text.toByteArray().size)
        assertArrayEquals(text.toByteArray(), copy(byteArrayOf(0xfe.toByte(), 0xff.toByte()) +
            text.toByteArray(Charsets.UTF_16BE)))
    }
    @Test fun rejectsMalformedUtf16AndEmptyBom() {
        listOf(byteArrayOf(0xfe.toByte(), 0xff.toByte()),
            byteArrayOf(0xfe.toByte(), 0xff.toByte(), 0xd8.toByte(), 0),
            byteArrayOf(0xff.toByte(), 0xfe.toByte(), 0, 0xdc.toByte()),
            byteArrayOf(0xfe.toByte(), 0xff.toByte(), 0)).forEach { bytes ->
            assertThrows(Exception::class.java) { copy(bytes) }
        }
    }
    @Test fun rejectsUtf16WhoseUtf8OutputExceedsLimit() {
        val bytes = byteArrayOf(0xfe.toByte(), 0xff.toByte()) +
            "가".repeat(SharedText.MAX_BYTES.toInt() / 3 + 1).toByteArray(Charsets.UTF_16BE)
        assertThrows(IllegalArgumentException::class.java) { copy(bytes) }
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
