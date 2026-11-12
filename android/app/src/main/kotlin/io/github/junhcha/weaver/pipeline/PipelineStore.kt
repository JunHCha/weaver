package io.github.junhcha.weaver.pipeline

import android.util.AtomicFile
import io.github.junhcha.weaver.schema.PipelineItem
import io.github.junhcha.weaver.schema.PipelineLocation
import io.github.junhcha.weaver.schema.WeaverCodec
import java.io.File
import java.security.MessageDigest

/**
 * One root has its own durable observations. Missing, partial and older files never delete them.
 */
class PipelineStore(private val directory: File) {
    private fun root(identity: String): File {
        val digest =
            MessageDigest.getInstance("SHA-256").digest(identity.toByteArray()).joinToString("") {
                "%02x".format(it)
            }
        return File(directory, digest).apply { check(exists() || mkdirs()) }
    }

    fun items(identity: String): List<PipelineItem> =
        root(identity)
            .listFiles()
            .orEmpty()
            .map {
                if (it.name.endsWith(".json.bak")) File(it.parentFile, it.name.removeSuffix(".bak"))
                else it
            }
            .filter { it.extension == "json" }
            .distinctBy { it.name }
            .mapNotNull { file ->
                runCatching {
                        WeaverCodec.json.decodeFromString<PipelineItem>(
                            AtomicFile(file).openRead().bufferedReader().use { it.readText() }
                        )
                    }
                    .getOrNull()
            }
            .sortedBy { it.sourceId }

    fun merge(identity: String, incoming: List<PipelineItem>) {
        val current = items(identity).associateBy { it.sourceId }.toMutableMap()
        for (item in incoming) {
            val previous = current[item.sourceId]
            if (
                previous != null &&
                    (item.revision <= previous.revision || item.captureId != previous.captureId)
            )
                continue
            val atomic = AtomicFile(File(root(identity), "${item.sourceId}.json"))
            val output = atomic.startWrite()
            try {
                output.write(WeaverCodec.encode(item).toByteArray())
                atomic.finishWrite(output)
                current[item.sourceId] = item
            } catch (error: Exception) {
                atomic.failWrite(output)
                throw error
            }
        }
    }
}

/** Only server-recorded transitions are replayed; first observation stays still. */
fun movement(previous: PipelineItem?, current: PipelineItem): List<PipelineLocation> {
    if (previous == null || previous == current) return emptyList()
    if (current.history.take(previous.history.size) != previous.history) return emptyList()
    val suffix = current.history.drop(previous.history.size)
    return if (suffix.isEmpty()) emptyList() else listOf(previous.location) + suffix
}
