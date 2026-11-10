package io.github.junhcha.weaver.courier

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import io.github.junhcha.weaver.schema.WeaverCodec
import io.github.junhcha.weaver.schema.v2.ReviewPackage
import io.github.junhcha.weaver.schema.v2.Stage
import io.github.junhcha.weaver.schema.v2.Verdict
import io.github.junhcha.weaver.review.ReviewStore
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.junit.rules.TemporaryFolder
import org.junit.Rule
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class VersionedCourierTest {
    @get:Rule val temporary = TemporaryFolder()
    private val id = "prp_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    private val pkg = ReviewPackage(2, id, "src_01ARZ3NDEKTSV4RRFFQ69G5FAV", 1, "요약", emptyList(), Stage.REVIEW, emptyMap())

    @Test fun scansAndQueuesV2WithoutChangingItsWireVersion() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val folder = MemoryFolder()
        val name = "$id.r1.json"
        folder.put("proposals", name, WeaverCodec.encode(pkg))
        CourierDatabase(context).use { db ->
            val scan = CourierClient(folder, db).scan()
            assertEquals(listOf(pkg), scan.packages)
            assertTrue(scan.proposals.isEmpty())
            assertTrue(scan.unreadable.isEmpty())
        }
        val verdict = Verdict(2, id, 1, Verdict.Action.APPROVE, emptyList(), null, null)
        val store = ReviewStore(temporary.newFolder())
        store.queue(folder, pkg, verdict)
        assertTrue(store.synchronize(folder))
        assertEquals(WeaverCodec.encode(verdict), folder.files.getValue("decisions/$name").decodeToString())
    }
}
