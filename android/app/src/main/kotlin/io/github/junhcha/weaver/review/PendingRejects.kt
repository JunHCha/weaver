package io.github.junhcha.weaver.review

import android.util.AtomicFile
import io.github.junhcha.weaver.schema.WeaverCodec
import kotlinx.serialization.Serializable
import kotlinx.serialization.builtins.ListSerializer
import java.io.File

/** A reject the user can still undo; it is sent once [fireAt] (epoch ms) has passed. */
@Serializable
data class PendingReject(val packageId: String, val revision: Long, val fireAt: Long)

/** Durable delayed-reject queue. Callers serialize access (ReviewRuntime holds CourierRuntime.lock). */
class PendingRejects(private val file: File) {
    private val serializer = ListSerializer(PendingReject.serializer())

    fun schedule(r: PendingReject) = write(all().filter { it.packageId != r.packageId } + r)

    fun cancel(packageId: String): Boolean {
        val current = all()
        val rest = current.filter { it.packageId != packageId }
        if (rest.size == current.size) return false
        write(rest)
        return true
    }

    fun all(): List<PendingReject> {
        if (!file.exists()) return emptyList()
        return runCatching {
            WeaverCodec.json.decodeFromString(serializer, AtomicFile(file).openRead().bufferedReader().use { it.readText() })
        }.getOrDefault(emptyList())
    }

    /** Due entries stay on disk until [remove]d, so a crash or failed send cannot lose them. */
    fun due(nowMs: Long): List<PendingReject> = all().filter { it.fireAt <= nowMs }

    fun remove(packageId: String, revision: Long) {
        val current = all()
        val rest = current.filterNot { it.packageId == packageId && it.revision == revision }
        if (rest.size != current.size) write(rest)
    }

    private fun write(items: List<PendingReject>) {
        file.parentFile?.mkdirs()
        val atomic = AtomicFile(file)
        val output = atomic.startWrite()
        try {
            output.write(WeaverCodec.json.encodeToString(serializer, items).toByteArray())
            atomic.finishWrite(output)
        } catch (error: Exception) {
            atomic.failWrite(output)
            throw error
        }
    }
}
