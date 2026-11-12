package io.github.junhcha.weaver.pipeline

import io.github.junhcha.weaver.schema.PipelineItem
import io.github.junhcha.weaver.schema.WeaverCodec
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [26, 36])
class PipelineCrashRecoveryTest {
    @get:Rule val temporary = TemporaryFolder()

    private fun fixture(): PipelineItem = WeaverCodec.json.decodeFromString(
        File(System.getProperty("weaver.schemaDir"), "examples/PipelineItem.json").readText(),
    )

    @Test fun interruptedWriteRecoversLastConfirmedObservation() {
        val directory = temporary.newFolder()
        val store = PipelineStore(directory)
        val confirmed = fixture()
        store.merge("example-root", listOf(confirmed))
        val base = directory.walkTopDown().single { it.isFile && it.extension == "json" }
        val backup = File(base.path + ".bak")
        backup.writeBytes(base.readBytes())
        base.writeText("{partial")

        assertEquals(listOf(confirmed), store.items("example-root"))
        assertFalse(backup.exists())
        assertEquals(confirmed, WeaverCodec.json.decodeFromString<PipelineItem>(base.readText()))
    }

    @Test fun crashBetweenBackupAndOpeningNewFileDoesNotHideItem() {
        val directory = temporary.newFolder()
        val store = PipelineStore(directory)
        val confirmed = fixture()
        store.merge("example-root", listOf(confirmed))
        val base = directory.walkTopDown().single { it.isFile && it.extension == "json" }
        check(base.renameTo(File(base.path + ".bak")))

        assertEquals(listOf(confirmed), store.items("example-root"))
        assertEquals(listOf(confirmed), store.items("example-root"))
    }
}
