package io.github.junhcha.weaver.shepherd.autoshare

import android.Manifest
import android.app.Application
import android.app.NotificationManager
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import androidx.test.core.app.ApplicationProvider
import androidx.work.Configuration
import androidx.work.ExistingWorkPolicy
import androidx.work.WorkManager
import androidx.work.testing.SynchronousExecutor
import androidx.work.testing.WorkManagerTestInitHelper
import io.github.junhcha.weaver.shepherd.CallStatus
import io.github.junhcha.weaver.shepherd.PendingCall
import io.github.junhcha.weaver.shepherd.RecordingMetadata
import io.github.junhcha.weaver.shepherd.ShepherdDatabase
import io.github.junhcha.weaver.shepherd.ShepherdRuntime
import java.time.ZoneId
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class AutoShareRuntimeTest {
    private lateinit var app: Application
    private lateinit var manager: NotificationManager
    private val started = mutableListOf<AutoShareJob>()
    private var starterResult = true
    private var env = RunEnvironment(screenOn = true, unlocked = true, callIdle = true)
    private val ready = RunEnvironment(screenOn = true, unlocked = true, callIdle = true)
    private val morning = 1_800_000_000_000L

    @Before fun setup() {
        app = ApplicationProvider.getApplicationContext()
        ShepherdRuntime.activatedCache = null
        app.deleteDatabase("shepherd.sqlite3")
        manager = app.getSystemService(NotificationManager::class.java)
        manager.cancelAll()
        WorkManagerTestInitHelper.initializeTestWorkManager(app, Configuration.Builder()
            .setExecutor(SynchronousExecutor()).build())
        AutoShareRuntime.active = null
        AutoShareRuntime.serviceEnabled = { true }
        AutoShareRuntime.environment = { env }
        AutoShareRuntime.starter = { _, job -> started.add(job.job); starterResult }
    }

    @After fun restore() {
        AutoShareRuntime.active = null
        AutoShareRuntime.starter = { _, job -> WeaverAutoShareService.begin(job) }
        AutoShareRuntime.environment = AutoShareRuntime::readEnvironment
        AutoShareRuntime.serviceEnabled = AutoShareRuntime::isServiceEnabledInSettings
        WorkManager.getInstance(app).cancelAllWork().result.get()
        WorkManagerTestInitHelper.closeWorkDatabase()
    }

    private fun stableCall(db: ShepherdDatabase, name: String = "통화 홍길동_261110_111103.m4a", uri: String = "content://media/external/audio/media/1") {
        db.activate(1_000)
        val rows = listOf(RecordingMetadata(uri, name, 480_000, 1, 2_000))
        db.observe(rows, 3_000, ZoneId.of("Asia/Seoul"))
        db.observe(rows, 33_000, ZoneId.of("Asia/Seoul"))
    }

    private fun enable() = ShepherdDatabase(app).use { it.setting("auto_share_enabled", "true") }

    // ---- migration, defaults ----

    @Test fun versionOneDatabaseUpgradesKeepingEverythingAndAddingTheAttemptLog() {
        val v1 = object : SQLiteOpenHelper(app, "shepherd.sqlite3", null, 1) {
            override fun onCreate(db: SQLiteDatabase) {
                db.execSQL("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execSQL("""CREATE TABLE calls (id TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL,
                    modified INTEGER NOT NULL, stable_since INTEGER NOT NULL, zone TEXT NOT NULL,
                    counterpart TEXT, occurred_at TEXT, state TEXT NOT NULL)""")
            }
            override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit
        }
        v1.writableDatabase.apply {
            execSQL("INSERT INTO settings VALUES ('activated_at','1000'),('notified_ids','a'),('reminded_day','2026-11-08')")
            execSQL("INSERT INTO calls VALUES ('u1','통화 홍길동_261110_111103.m4a',10,1,2,'UTC',NULL,NULL,'UNSHARED')")
            execSQL("INSERT INTO calls VALUES ('u2','통화 임꺽정_261109_101010.m4a',10,1,2,'UTC',NULL,NULL,'PROCESSED')")
            close()
        }
        ShepherdDatabase(app).use { db ->
            assertEquals(1_000L, db.activatedAt())
            assertEquals("a", db.setting("notified_ids"))
            assertEquals("2026-11-08", db.setting("reminded_day"))
            assertEquals(CallStatus.UNSHARED, db.pendingCalls().single().status)
            assertEquals(CallStatus.PROCESSED, db.calls().single { it.id == "u2" }.status)
            assertNull(db.lastAutoShare())
            assertEquals(AutoShareOutcome.FAILED, run { db.recordAutoShareFailure("u1", AutoStep.PICK_APP, AutoError.APP_NOT_IN_SHEET, 5); db.autoShareAttempt("u1")!!.outcome })
            val columns = db.readableDatabase.rawQuery("PRAGMA table_info(auto_share_attempts)", null).use { c ->
                buildSet { while (c.moveToNext()) add(c.getString(1)) }
            }
            // Only identifiers, counters and enum names: no screen text, script text or contact names.
            assertEquals(setOf("call_id", "attempts", "last_step", "error_type", "outcome", "updated_at"), columns)
        }
    }

    @Test fun freshDatabaseHasTheAttemptLogToo() {
        ShepherdDatabase(app).use { assertNull(it.lastAutoShare()) }
    }

    @Test fun autoShareIsOffByDefault() {
        ShepherdDatabase(app).use { stableCall(it) }
        assertFalse(AutoShareRuntime.isEnabled(app))
        assertFalse(AutoShareRuntime.status(app).enabled)
        assertEquals(StartResult.DISABLED, AutoShareRuntime.maybeStart(app, morning))
        assertTrue(started.isEmpty())
    }

    // ---- run conditions and retry scheduling ----

    @Test fun lockedScreenOffAndInCallBlockTheRunAndReserveARetry() {
        ShepherdDatabase(app).use { stableCall(it) }
        enable()
        listOf(RunEnvironment(false, true, true), RunEnvironment(true, false, true), RunEnvironment(true, true, false)).forEach { blocked ->
            env = blocked
            WorkManager.getInstance(app).cancelAllWork().result.get()
            assertEquals(StartResult.BLOCKED, AutoShareRuntime.maybeStart(app, morning))
            val work = WorkManager.getInstance(app).getWorkInfosForUniqueWork("shepherd-autoshare-retry").get()
            assertEquals(1, work.count { !it.state.isFinished })
        }
        assertTrue(started.isEmpty())
        assertNull(AutoShareRuntime.active)
    }

    @Test fun readyDeviceStartsTheOldestStableCallOnceAtATime() {
        ShepherdDatabase(app).use { stableCall(it) }
        enable()
        assertEquals(StartResult.STARTED, AutoShareRuntime.maybeStart(app, morning))
        assertEquals(AutoShareJob("content://media/external/audio/media/1", "통화 홍길동_261110_111103", 480_000), started.single())
        assertEquals(StartResult.BUSY, AutoShareRuntime.maybeStart(app, morning))
        assertEquals(1, started.size)
    }

    @Test fun disabledServiceOrMissingServiceDoesNotConsumeTheCall() {
        ShepherdDatabase(app).use { stableCall(it) }
        enable()
        AutoShareRuntime.serviceEnabled = { false }
        assertEquals(StartResult.SERVICE_OFF, AutoShareRuntime.maybeStart(app, morning))
        AutoShareRuntime.serviceEnabled = { true }
        starterResult = false
        assertEquals(StartResult.NOT_STARTED, AutoShareRuntime.maybeStart(app, morning))
        assertNull(AutoShareRuntime.active)
        starterResult = true
        assertEquals(StartResult.STARTED, AutoShareRuntime.maybeStart(app, morning))
    }

    @Test fun callsStillStabilizingAreNotQueued() {
        ShepherdDatabase(app).use { db ->
            db.activate(1_000)
            db.observe(listOf(RecordingMetadata("u", "통화 홍길동_261110_111103.m4a", 10, 1, 2_000)), 3_000, ZoneId.of("UTC"))
        }
        enable()
        assertEquals(StartResult.NO_CANDIDATE, AutoShareRuntime.maybeStart(app, morning))
    }

    // ---- failures, retries, hand-over to the manual notice ----

    private fun fail(now: Long) {
        AutoShareRuntime.active = AutoShareRuntime.ActiveJob(started.last())
        AutoShareRuntime.onFinished(app, started.last(), Progress.Failed(AutoError.APP_NOT_IN_SHEET, AutoStep.PICK_APP), now)
    }

    @Test fun threeFailuresStopAutoShareAndHandOverToTheManualNotice() {
        shadowOf(app).grantPermissions(Manifest.permission.POST_NOTIFICATIONS)
        ShepherdDatabase(app).use { stableCall(it) }
        enable()
        val delay = AutoSharePolicy.RETRY_DELAY_MS
        var now = morning
        repeat(2) { n ->
            assertEquals(StartResult.STARTED, AutoShareRuntime.maybeStart(app, now))
            fail(now)
            // While auto share is still trying, the manual notice stays quiet.
            ShepherdDatabase(app).use { ShepherdRuntime.notifyPending(app, it, now) }
            assertTrue(shadowOf(manager).allNotifications.isEmpty())
            // Retry is held back for the delay, then allowed.
            assertEquals(StartResult.NO_CANDIDATE, AutoShareRuntime.maybeStart(app, now + delay - 1))
            assertEquals(n + 1, ShepherdDatabase(app).use { it.lastAutoShare()!!.attempts })
            assertEquals(1, WorkManager.getInstance(app).getWorkInfosForUniqueWork("shepherd-autoshare-retry").get()
                .count { !it.state.isFinished })
            now += delay
        }
        assertEquals(StartResult.STARTED, AutoShareRuntime.maybeStart(app, now))
        fail(now)
        val last = ShepherdDatabase(app).use { it.lastAutoShare()!! }
        assertEquals(AutoShareOutcome.GAVE_UP, last.outcome)
        assertEquals(3, last.attempts)
        assertEquals("PICK_APP", last.lastStep)
        assertEquals("APP_NOT_IN_SHEET", last.error)
        assertEquals(StartResult.NO_CANDIDATE, AutoShareRuntime.maybeStart(app, now + 100 * delay))
        ShepherdDatabase(app).use { ShepherdRuntime.notifyPending(app, it, now) }
        assertEquals(1, shadowOf(manager).allNotifications.size)
    }

    @Test fun successSilencesTheNoticeAndIsNeverRetried() {
        shadowOf(app).grantPermissions(Manifest.permission.POST_NOTIFICATIONS)
        ShepherdDatabase(app).use { stableCall(it) }
        enable()
        assertEquals(StartResult.STARTED, AutoShareRuntime.maybeStart(app, morning))
        AutoShareRuntime.onFinished(app, started.single(), Progress.Succeeded, morning)
        assertNull(AutoShareRuntime.active)
        assertEquals(AutoShareOutcome.SUCCESS, ShepherdDatabase(app).use { it.lastAutoShare()!!.outcome })
        // Courier has not synced yet, so the call is still UNSHARED, but nobody needs to share it by hand.
        ShepherdDatabase(app).use { ShepherdRuntime.notifyPending(app, it, morning) }
        assertTrue(shadowOf(manager).allNotifications.isEmpty())
        assertEquals(StartResult.NO_CANDIDATE, AutoShareRuntime.maybeStart(app, morning + 1_000_000_000))
    }

    @Test fun manualNoticeIsUnchangedWhileAutoShareIsOffOrItsServiceIsNotEnabled() {
        shadowOf(app).grantPermissions(Manifest.permission.POST_NOTIFICATIONS)
        ShepherdDatabase(app).use { stableCall(it) }
        AutoShareRuntime.serviceEnabled = { false }
        enable()
        ShepherdDatabase(app).use { ShepherdRuntime.notifyPending(app, it, morning) }
        assertEquals(1, shadowOf(manager).allNotifications.size)
    }

    @Test fun enablingAgainGivesGaveUpCallsAFreshStart() {
        ShepherdDatabase(app).use { db ->
            stableCall(db)
            repeat(3) { db.recordAutoShareFailure("content://media/external/audio/media/1", AutoStep.PICK_APP, AutoError.STEP_TIMEOUT, it.toLong()) }
            assertEquals(AutoShareOutcome.GAVE_UP, db.lastAutoShare()!!.outcome)
        }
        AutoShareRuntime.setEnabled(app, true)
        assertTrue(AutoShareRuntime.isEnabled(app))
        // Enabling kicks a scan worker (synchronous in tests), which already started the call again.
        assertEquals(1, started.size)
        assertEquals(StartResult.BUSY, AutoShareRuntime.maybeStart(app, morning))
    }

    // ---- hand-off to ShareActivity ----

    @Test fun onlyTheRunningJobsOwnTranscriptIsSubmittedUnattended() {
        val job = AutoShareJob("c1", "통화 홍길동_261110_111103", 1)
        val pending = listOf(PendingCall("c1", null, null, "통화 홍길동_261110_111103.m4a"))
        assertNull(AutoShareRuntime.autoSubmitCallId("통화 홍길동_261110_111103_original.txt", pending))
        AutoShareRuntime.active = AutoShareRuntime.ActiveJob(job)
        assertEquals("c1", AutoShareRuntime.autoSubmitCallId("통화 홍길동_261110_111103_original.txt", pending))
        assertEquals("c1", AutoShareRuntime.autoSubmitCallId("통화 홍길동_261110_111103_original (1).txt", pending))
        assertNull(AutoShareRuntime.autoSubmitCallId("통화 임꺽정_261109_101010_original.txt", pending))
        assertNull(AutoShareRuntime.autoSubmitCallId("memo.txt", pending))
        assertNull(AutoShareRuntime.autoSubmitCallId("통화 홍길동_261110_111103_original.txt", emptyList()))
        AutoShareRuntime.onSubmitted("other")
        assertFalse(AutoShareRuntime.active!!.submitted)
        AutoShareRuntime.onSubmitted("c1")
        assertTrue(AutoShareRuntime.active!!.submitted)
    }

    @Test fun retryReservationUsesOneUniqueWork() {
        AutoShareRuntime.scheduleRetry(app, 1_000, ExistingWorkPolicy.REPLACE)
        AutoShareRuntime.scheduleRetry(app, 1_000, ExistingWorkPolicy.KEEP)
        val work = WorkManager.getInstance(app).getWorkInfosForUniqueWork("shepherd-autoshare-retry").get()
        assertEquals(1, work.count { !it.state.isFinished })
    }
}
