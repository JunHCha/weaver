package io.github.junhcha.weaver.shepherd

import org.junit.Assert.*
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.File
import java.nio.charset.CharacterCodingException

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
    @Test fun preservesCrLfWhitespaceAndDropsOnlyRepeatedLeadingBom() {
        val text = "가짜 \uFEFF내용 \r\n\r\n끝 \n"
        assertArrayEquals(text.toByteArray(), copy(byteArrayOf(0xff.toByte(), 0xfe.toByte()) +
            "\uFEFF$text".toByteArray(Charsets.UTF_16LE)))
        assertArrayEquals("\uFEFF$text".toByteArray(), copy(byteArrayOf(0xfe.toByte(), 0xff.toByte()) +
            "\uFEFF\uFEFF$text".toByteArray(Charsets.UTF_16BE)))
    }
    @Test fun keepsSurrogatePairsSplitAcrossReadBoundaries() {
        (4088..4100).forEach { prefix ->
            val text = "a".repeat(prefix) + "😀가\n"
            val bytes = byteArrayOf(0xfe.toByte(), 0xff.toByte()) + text.toByteArray(Charsets.UTF_16BE)
            assertArrayEquals(text.toByteArray(), copy(bytes))
            // Odd-sized reads also split code units and BOMs between reads.
            val file = File.createTempFile("share", ".txt")
            try {
                SharedText.copy(object : java.io.FilterInputStream(ByteArrayInputStream(bytes)) {
                    override fun read(b: ByteArray, off: Int, len: Int) = super.read(b, off, minOf(len, 3))
                }, file)
                assertArrayEquals(text.toByteArray(), file.readBytes())
            } finally { file.delete() }
        }
    }
    @Test fun acceptsUtf16WhoseUtf8OutputIsExactlyLimit() {
        val text = "가".repeat(SharedText.MAX_BYTES.toInt() / 3) + "a"
        assertEquals(SharedText.MAX_BYTES.toInt(), text.toByteArray().size)
        assertArrayEquals(text.toByteArray(), copy(byteArrayOf(0xfe.toByte(), 0xff.toByte()) +
            text.toByteArray(Charsets.UTF_16BE)))
    }
    @Test fun rejectsEmptyBomAndNulContent() {
        listOf(byteArrayOf(0xfe.toByte(), 0xff.toByte()), byteArrayOf(0xff.toByte(), 0xfe.toByte()),
            byteArrayOf(0xff.toByte(), 0xfe.toByte(), 0, 0)).forEach { bytes ->
            val file = File.createTempFile("share", ".txt")
            try {
                assertThrows(IllegalArgumentException::class.java) { SharedText.copy(ByteArrayInputStream(bytes), file) }
                assertFalse(file.exists())
            } finally { file.delete() }
        }
    }
    @Test fun rejectsTrailingOrUnpairedUtf16AfterRealContent() {
        fun bytes(vararg values: Int) = ByteArray(values.size) { values[it].toByte() }
        // Each starts with U+AC00 so an encoder that silently drops the tail would pass validation.
        listOf(bytes(0xfe, 0xff, 0xac, 0x00, 0xd8, 0x00), // trailing high surrogate
            bytes(0xfe, 0xff, 0xac, 0x00, 0x00), // trailing odd byte
            bytes(0xff, 0xfe, 0x00, 0xac, 0x00), // trailing odd byte (LE)
            bytes(0xfe, 0xff, 0xac, 0x00, 0xd8, 0x00, 0x00, 0x41), // high then non-low
            bytes(0xfe, 0xff, 0xac, 0x00, 0xdc, 0x00, 0x00, 0x41), // lone low in the middle
            bytes(0xff, 0xfe, 0x00, 0xac, 0x00, 0xdc, 0x41, 0x00), // lone low (LE)
            bytes(0xfe, 0xff, 0xac, 0x00, 0xd8, 0x00, 0xd8, 0x00, 0xdc, 0x00)).forEach { input ->
            val file = File.createTempFile("share", ".txt")
            try {
                assertThrows(CharacterCodingException::class.java) { SharedText.copy(ByteArrayInputStream(input), file) }
                assertFalse(file.exists())
            } finally { file.delete() }
        }
    }
    @Test fun rejectsUtf16InputOverLimitEvenWhenUtf8OutputFits() {
        val bytes = byteArrayOf(0xfe.toByte(), 0xff.toByte()) +
            "a".repeat(SharedText.MAX_BYTES.toInt() / 2).toByteArray(Charsets.UTF_16BE)
        assertEquals(SharedText.MAX_BYTES + 2, bytes.size.toLong())
        assertThrows(IllegalArgumentException::class.java) { copy(bytes) }
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
