package io.github.junhcha.weaver.courier

import android.content.Context
import android.net.Uri
import androidx.test.core.app.ApplicationProvider
import io.github.junhcha.weaver.schema.CaptureKind
import io.github.junhcha.weaver.schema.Channel
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [26, 36])
class CourierRuntimeTest {
    @Test fun prepareSnapshotsOriginalAndReusesIdAfterRestartWithoutSelectedTree() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        File(context.filesDir, "courier.sqlite3").delete()
        context.getSharedPreferences("courier", Context.MODE_PRIVATE).edit().clear().commit()
        val original = File(context.cacheDir, "synthetic-original.txt").apply { writeText("synthetic") }
        try {
            val first = CourierRuntime.prepare(context, Uri.fromFile(original), CaptureKind.TEXT, Channel.SHARE_TEXT, "txt")
            val other = File(context.cacheDir, "another-synthetic.txt").apply { writeText("synthetic") }
            try {
                val repeated = CourierRuntime.prepare(context, Uri.fromFile(other), CaptureKind.TEXT, Channel.SHARE_TEXT, "txt")
                assertEquals(first, repeated)
                assertTrue(first.captureId.matches(Regex("cap_${CourierRules.ulid}")))
                CourierDatabase(context).use { db ->
                    assertEquals(1, db.captures().size)
                    assertEquals("PENDING", db.captures().single().state)
                    assertEquals("synthetic", File(db.captures().single().stagedPath).readText())
                }
                assertTrue(original.exists())
                assertEquals(1, File(context.filesDir, "courier-staging").listFiles()!!.count { !it.name.startsWith(".") })
            } finally { other.delete() }
        } finally { original.delete() }
    }
}
