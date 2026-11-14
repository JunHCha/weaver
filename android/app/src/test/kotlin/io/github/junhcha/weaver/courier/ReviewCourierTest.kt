package io.github.junhcha.weaver.courier

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import io.github.junhcha.weaver.schema.WeaverCodec
import io.github.junhcha.weaver.schema.Action
import io.github.junhcha.weaver.schema.ActionKind
import io.github.junhcha.weaver.schema.ActionResult
import io.github.junhcha.weaver.schema.CalendarPayload
import io.github.junhcha.weaver.schema.ReviewPackage
import io.github.junhcha.weaver.schema.Stage
import io.github.junhcha.weaver.schema.Verdict
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
class ReviewCourierTest {
    @get:Rule val temporary = TemporaryFolder()
    private val id = "pkg_0123456789abcdef"
    private val pkg = ReviewPackage(2, id, "src_01ARZ3NDEKTSV4RRFFQ69G5FAV", 1, "요약", emptyList(), Stage.REVIEW, emptyMap())

    private val actionId = "act_0123456789abcdef0123456789abcdef"
    private val action = Action(
        2, actionId, ActionKind.REGISTER, "thread", 3, 1,
        CalendarPayload("2026-01-01T09:00:00+09:00", null, "Asia/Seoul", "회의"), null)

    @Test fun scansActionsAndIgnoresTheRetiredScheduleDirectory() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val folder = MemoryFolder()
        folder.put("actions", "$actionId.json", WeaverCodec.encode(action))
        folder.put("actions", ".$actionId.json.0123456789abcdef.tmp", "{")
        // A directory the older server wrote under proposals/ is not a review package.
        folder.put("proposals/schedules", "thread.json", "{}")
        CourierDatabase(context).use { db ->
            val scan = CourierClient(folder, db).scan()
            assertEquals(listOf(action), scan.actions)
            assertTrue(scan.unreadable.isEmpty())
        }
    }

    @Test fun invalidOrMislabelledActionFilesAreSkippedWithoutBlockingOthers() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val folder = MemoryFolder()
        folder.put("actions", "$actionId.json", WeaverCodec.encode(action))
        folder.put("actions", "act_ffffffffffffffffffffffffffffffff.json", WeaverCodec.encode(action))
        folder.put("actions", "act_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee.json", WeaverCodec.encode(action).replace("\"schema_version\":2", "\"schema_version\":3"))
        CourierDatabase(context).use { db ->
            assertEquals(listOf(action), CourierClient(folder, db).scan().actions)
        }
    }

    @Test fun publishesResultsOnceAndNeverOverwritesADifferentOne() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val folder = MemoryFolder()
        val result = ActionResult(2, actionId, ActionResult.Outcome.COMPLETED, "content://com.android.calendar/events/7", 1, "2026-01-01T00:00:00Z", null)
        CourierDatabase(context).use { db ->
            val client = CourierClient(folder, db)
            client.publishActionResult(result)
            client.publishActionResult(result)
            assertEquals(WeaverCodec.encode(result), folder.files.getValue("action-results/$actionId.json").decodeToString())
            assertThrows(IllegalStateException::class.java) { client.publishActionResult(result.copy(attempts = 2)) }
        }
    }

    @Test fun scansAndQueuesReviewPackagesAndVerdicts() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val folder = MemoryFolder()
        val name = "$id.r1.json"
        folder.put("proposals", name, WeaverCodec.encode(pkg))
        CourierDatabase(context).use { db ->
            val scan = CourierClient(folder, db).scan()
            assertEquals(listOf(pkg), scan.packages)
            assertTrue(scan.unreadable.isEmpty())
        }
        val verdict = Verdict(2, id, 1, Verdict.Action.APPROVE, emptyList(), null, null)
        val store = ReviewStore(temporary.newFolder())
        store.queue(folder, pkg, verdict)
        assertTrue(store.synchronize(folder))
        assertEquals(WeaverCodec.encode(verdict), folder.files.getValue("decisions/$name").decodeToString())
    }
}
