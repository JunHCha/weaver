package io.github.junhcha.weaver.courier

import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import kotlin.concurrent.thread

object CourierDiagnostics {
    /** Local concurrent-reader probe. A pass still needs the Fold/Syncthing remote-reader gate. */
    fun atomicity(folder: CourierFolder): String {
        val name = "probe-${UUID.randomUUID()}.bin"
        val expectedSize = 16L * 1024 * 1024
        val stop = AtomicBoolean(false)
        val observed = AtomicInteger()
        val failure = AtomicReference<Throwable?>()
        val reader = thread(name = "courier-atomic-reader") {
            try {
                while (!stop.get()) {
                    if (folder.list("outbox").any { it.name == name }) {
                        folder.read("outbox", name).use { input ->
                            val buffer = ByteArray(64 * 1024)
                            var size = 0L
                            while (true) {
                                val count = input.read(buffer)
                                if (count < 0) break
                                check((0 until count).all { buffer[it] == 0x5a.toByte() }) { "partial_content" }
                                size += count
                            }
                            check(size == expectedSize) { "partial_final_file" }
                            observed.incrementAndGet()
                        }
                    }
                    Thread.sleep(5)
                }
            } catch (error: Throwable) { failure.set(error) }
        }
        try {
            folder.publish("outbox", name) { output ->
                val chunk = ByteArray(64 * 1024) { 0x5a }
                repeat(256) { output.write(chunk); Thread.sleep(2) }
            }
            // Keep final name visible long enough for at least one complete read.
            reader.join(1000)
        } finally {
            stop.set(true)
            reader.join(10_000)
            if (!reader.isAlive) folder.delete("outbox", name)
        }
        check(!reader.isAlive) { "atomicity_reader_timeout" }
        failure.get()?.let { throw IllegalStateException("atomicity_failed", it) }
        check(observed.get() > 0) { "atomicity_inconclusive" }
        return "16 MiB local reader: ${observed.get()} complete reads, no partial final file. Fold/Syncthing remote test pending."
    }
}
