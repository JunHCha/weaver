package io.github.junhcha.weaver.shepherd

import android.content.Context
import android.content.Intent
import android.net.Uri
import io.github.junhcha.weaver.courier.CourierClient
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.io.InputStreamReader
import java.nio.charset.CodingErrorAction
import java.util.UUID

/** Sender names and declared sizes are deliberately never used as paths or limits. */
object SharedText {
    const val MAX_BYTES = 10L * 1024 * 1024

    fun receive(context: Context, intent: Intent): File {
        require(intent.action == Intent.ACTION_SEND) { "한 번에 텍스트 한 항목만 공유하세요." }
        require(intent.type?.substringBefore(';')?.trim()?.lowercase() == "text/plain") {
            "text/plain 텍스트만 지원합니다. 음성·오디오 파일은 받을 수 없습니다."
        }
        @Suppress("DEPRECATION")
        val stream = intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM)
        require(intent.clipData == null || intent.clipData!!.itemCount <= 1) { "한 번에 한 항목만 공유하세요." }
        require(stream == null || stream.scheme == "content") { "공유 파일은 content URI여야 합니다." }
        if (stream != null) {
            val resolvedType = context.contentResolver.getType(stream)?.substringBefore(';')?.trim()?.lowercase()
            require(resolvedType == null || resolvedType == "text/plain") { "text/plain 텍스트 파일만 지원합니다." }
        }
        val text = intent.getCharSequenceExtra(Intent.EXTRA_TEXT)
        require(stream == null || text == null) { "파일 또는 본문 중 한 항목만 공유하세요." }
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
                    val bounded = object : java.io.OutputStream() {
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
                    java.io.OutputStreamWriter(bounded, encoder).use { it.append(text) }
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

    fun copy(input: InputStream, file: File) {
        try {
            FileOutputStream(file).use { output ->
                val buffer = ByteArray(64 * 1024)
                var size = 0L
                while (true) {
                    val count = input.read(buffer)
                    if (count < 0) break
                    size += count
                    require(size <= MAX_BYTES) { "텍스트는 10 MiB 이하여야 합니다." }
                    output.write(buffer, 0, count)
                }
                output.fd.sync()
            }
            validate(file)
        } catch (error: Exception) {
            file.delete()
            throw error
        }
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
                    require(chars[index] != '\u0000') { "UTF-16은 지원하지 않습니다. UTF-8로 공유하세요." }
                    if (!chars[index].isWhitespace() && chars[index] != '\uFEFF') meaningful = true
                }
            }
        }
        require(meaningful) { "공유한 텍스트가 비어 있습니다." }
    }

    fun digest(file: File): String = file.inputStream().use { CourierClient.digest(it).first }
}
