package io.github.junhcha.weaver.shepherd

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.DocumentsContract
import android.provider.OpenableColumns
import io.github.junhcha.weaver.courier.CourierRuntime
import io.github.junhcha.weaver.courier.CourierClient
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.io.InputStreamReader
import java.io.OutputStream
import java.io.OutputStreamWriter
import java.io.PushbackInputStream
import java.nio.charset.CodingErrorAction
import java.nio.charset.MalformedInputException
import java.util.UUID

/** Sender names and declared sizes are deliberately never used as paths or limits. */
object SharedText {
    const val MAX_BYTES = 10L * 1024 * 1024
    private const val TEXT_PLAIN = "text/plain"
    private const val APPLICATION_TXT = "application/txt"

    fun receive(context: Context, intent: Intent): File {
        require(intent.action == Intent.ACTION_SEND || intent.action == Intent.ACTION_SEND_MULTIPLE) {
            "한 번에 텍스트 한 항목만 공유하세요."
        }
        val type = intent.type?.substringBefore(';')?.trim()?.lowercase()
        require(type == TEXT_PLAIN || type == APPLICATION_TXT) {
            "text/plain 또는 application/txt 텍스트만 지원합니다. 음성·오디오 파일은 받을 수 없습니다."
        }
        val stream = streamUri(intent)
        require(intent.clipData == null || intent.clipData!!.itemCount <= 1) { "한 번에 한 항목만 공유하세요." }
        require(stream == null || stream.scheme == "content") { "공유 파일은 content URI여야 합니다." }
        if (stream != null) {
            require(!privateSource(context, stream)) { "앱 내부 또는 Courier 폴더의 파일은 공유 입력으로 받을 수 없습니다." }
            val resolvedType = context.contentResolver.getType(stream)?.substringBefore(';')?.trim()?.lowercase()
            require(resolvedType == null || resolvedType == TEXT_PLAIN || resolvedType == APPLICATION_TXT) {
                "text/plain 텍스트 파일만 지원합니다."
            }
            if (type == APPLICATION_TXT || resolvedType == APPLICATION_TXT) {
                require(displayName(context, intent)?.lowercase()?.endsWith(".txt") == true) {
                    "확장자가 .txt인 텍스트 파일만 지원합니다."
                }
            }
        }
        // A file stream wins; the accompanying EXTRA_TEXT (e.g. a preview) is ignored.
        val text = if (stream == null) intent.getCharSequenceExtra(Intent.EXTRA_TEXT) else null
        val target = File(context.filesDir, "shepherd-shares").apply { mkdirs() }
        target.listFiles()?.filter { System.currentTimeMillis() - it.lastModified() > 24 * 60 * 60 * 1000L }
            ?.forEach { it.delete() }
        val file = File(target, "${UUID.randomUUID()}.txt")
        try {
            if (stream != null) {
                val input = context.contentResolver.openInputStream(stream)
                    ?: error("공유 파일을 읽을 수 없습니다. 다시 공유하세요.")
                input.use { copy(it, file) }
            } else {
                require(text != null) { "공유한 텍스트가 없습니다." }
                // Encode incrementally; the bounded stream also protects EXTRA_TEXT inputs.
                val encoder = Charsets.UTF_8.newEncoder().onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT)
                FileOutputStream(file).use { output ->
                    OutputStreamWriter(bounded(output), encoder).use { it.append(text) }
                    output.fd.sync()
                }
                validate(file)
            }
            return file
        } catch (error: Exception) {
            file.delete()
            throw error
        }
    }

    internal fun privateSource(context: Context, uri: Uri): Boolean {
        val authority = uri.authority ?: return true
        if (authority == context.packageName || authority.startsWith("${context.packageName}.")) return true
        val courier = CourierRuntime.tree(context) ?: return false
        if (uri.authority != courier.authority) return false
        // Reject the entire selected tree, including document-only URI aliases.
        val root = runCatching { DocumentsContract.getTreeDocumentId(courier) }.getOrNull() ?: return true
        val document = runCatching { DocumentsContract.getDocumentId(uri) }.getOrNull()
        val tree = runCatching { DocumentsContract.getTreeDocumentId(uri) }.getOrNull()
        return listOfNotNull(document, tree).any { it == root || it.startsWith("$root/") } ||
            (document == null && tree == null)
    }

    /** Keep the original intent and its ClipData/read grant while selecting exactly one URI. */
    @Suppress("DEPRECATION")
    private fun streamUri(intent: Intent): Uri? {
        if (intent.action == Intent.ACTION_SEND_MULTIPLE) {
            val streams = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
            } else {
                intent.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM)
            }
            require(streams?.size == 1) { "텍스트 파일을 한 개만 선택해 공유하세요." }
            return requireNotNull(streams.single()) { "공유한 텍스트 파일이 없습니다." }
        }
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
        } else {
            intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM)
        }
    }

    /** Untrusted display metadata is only a selection hint, never a local path. */
    fun displayName(context: Context, intent: Intent): String? {
        val uri = streamUri(intent) ?: return null
        return runCatching {
            context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { cursor ->
                val column = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (column >= 0 && cursor.moveToFirst()) cursor.getString(column) else null
            }
        }.getOrNull()
    }

    private fun bounded(output: OutputStream): OutputStream = object : OutputStream() {
        var size = 0L
        override fun write(value: Int) {
            require(++size <= MAX_BYTES) { "텍스트는 10 MiB 이하여야 합니다." }
            output.write(value)
        }
        override fun write(bytes: ByteArray, off: Int, len: Int) {
            size += len
            require(size <= MAX_BYTES) { "텍스트는 10 MiB 이하여야 합니다." }
            output.write(bytes, off, len)
        }
    }

    fun copy(input: InputStream, file: File) {
        try {
            // Bound both sender bytes and UTF-8 bytes; never trust provider SIZE metadata.
            val limited = object : java.io.FilterInputStream(input) {
                var size = 0L
                private fun count(n: Int): Int {
                    if (n > 0) size += n
                    require(size <= MAX_BYTES) { "텍스트는 10 MiB 이하여야 합니다." }
                    return n
                }
                override fun read(): Int = super.read().also { if (it >= 0) count(1) }
                override fun read(bytes: ByteArray, off: Int, len: Int): Int = count(`in`.read(bytes, off, len))
            }
            val source = PushbackInputStream(limited, 2)
            val first = source.read()
            val second = source.read()
            val charset = when {
                first == 0xfe && second == 0xff -> Charsets.UTF_16BE
                first == 0xff && second == 0xfe -> Charsets.UTF_16LE
                else -> null
            }
            FileOutputStream(file).use { output ->
                val target = bounded(output)
                if (charset == null) {
                    if (second >= 0) source.unread(second)
                    if (first >= 0) source.unread(first)
                    source.copyTo(target, 64 * 1024)
                } else {
                    // The source BOM is consumed above. UTF-8 output never gets a BOM.
                    transcodeUtf16(source, charset == Charsets.UTF_16BE, target)
                }
                output.fd.sync()
            }
            validate(file)
        } catch (error: Exception) {
            file.delete()
            throw error
        }
    }

    /**
     * Decodes UTF-16 by hand: Android's ICU decoder behind InputStreamReader may drop a trailing
     * odd byte or unpaired high surrogate instead of reporting it, so every unit is checked here.
     */
    private fun transcodeUtf16(input: InputStream, bigEndian: Boolean, output: OutputStream) {
        val bytes = ByteArray(8192)
        val out = ByteArray(bytes.size * 2 + 4)
        var pending = -1
        var high = -1
        var first = true
        while (true) {
            val count = input.read(bytes)
            if (count < 0) break
            var size = 0
            for (index in 0 until count) {
                val byte = bytes[index].toInt() and 0xff
                if (pending < 0) { pending = byte; continue }
                val unit = if (bigEndian) (pending shl 8) or byte else (byte shl 8) or pending
                pending = -1
                val point = when {
                    high >= 0 -> {
                        if (unit !in 0xdc00..0xdfff) throw MalformedInputException(2)
                        (0x10000 + ((high - 0xd800) shl 10) + (unit - 0xdc00)).also { high = -1 }
                    }
                    unit in 0xd800..0xdbff -> { high = unit; -1 }
                    unit in 0xdc00..0xdfff -> throw MalformedInputException(2)
                    // A second BOM is content in UTF-16, but would read as a UTF-8 BOM after conversion.
                    first && unit == 0xfeff -> -1
                    else -> unit
                }
                first = false
                when {
                    point < 0 -> {}
                    point < 0x80 -> out[size++] = point.toByte()
                    point < 0x800 -> {
                        out[size++] = (0xc0 or (point shr 6)).toByte()
                        out[size++] = (0x80 or (point and 0x3f)).toByte()
                    }
                    point < 0x10000 -> {
                        out[size++] = (0xe0 or (point shr 12)).toByte()
                        out[size++] = (0x80 or ((point shr 6) and 0x3f)).toByte()
                        out[size++] = (0x80 or (point and 0x3f)).toByte()
                    }
                    else -> {
                        out[size++] = (0xf0 or (point shr 18)).toByte()
                        out[size++] = (0x80 or ((point shr 12) and 0x3f)).toByte()
                        out[size++] = (0x80 or ((point shr 6) and 0x3f)).toByte()
                        out[size++] = (0x80 or (point and 0x3f)).toByte()
                    }
                }
            }
            output.write(out, 0, size)
        }
        if (pending >= 0) throw MalformedInputException(1)
        if (high >= 0) throw MalformedInputException(2)
        output.flush()
    }

    fun validate(file: File) {
        require(file.length() in 1..MAX_BYTES) { "비어 있거나 너무 큰 텍스트입니다." }
        val decoder = Charsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT)
        var meaningful = false
        InputStreamReader(file.inputStream(), decoder).use { reader ->
            val chars = CharArray(8192)
            while (true) {
                val count = reader.read(chars)
                if (count < 0) break
                for (index in 0 until count) {
                    require(chars[index] != '\u0000') { "BOM 없는 UTF-16이나 NUL 문자는 지원하지 않습니다. UTF-8로 공유하세요." }
                    if (!chars[index].isWhitespace() && chars[index] != '\uFEFF') meaningful = true
                }
            }
        }
        require(meaningful) { "공유한 텍스트가 비어 있습니다." }
    }

    fun digest(file: File): String = file.inputStream().use { CourierClient.digest(it).first }
}
