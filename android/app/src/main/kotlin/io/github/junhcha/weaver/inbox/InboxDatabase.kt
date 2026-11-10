package io.github.junhcha.weaver.inbox

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import io.github.junhcha.weaver.courier.CourierRules
import io.github.junhcha.weaver.schema.Alert
import io.github.junhcha.weaver.schema.ReviewPackage
import java.io.File

/**
 * Local list of review packages and change alerts shared by notifications and the widget.
 * Rows are never deleted: their states are the per-ID notification history that keeps resynced
 * or restarted scans from notifying twice. Only the first summary line and the alert message are
 * stored; the full package stays in the Courier folder for the review screen.
 *
 * Deleting an alert file is irreversible (Syncthing propagates it to the PC), so it happens only for
 * DISMISSED alerts and for alerts RESOLVED in this very run, and only on a trusted listing.
 */
class InboxDatabase(context: Context) : SQLiteOpenHelper(
    context, File(context.filesDir, "inbox.sqlite3").absolutePath, null, 3,
), java.io.Closeable {
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execSQL("""CREATE TABLE reviews (
            package_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, summary TEXT NOT NULL,
            stage TEXT NOT NULL, revised INTEGER NOT NULL, state TEXT NOT NULL,
            first_seen INTEGER NOT NULL, changed_at INTEGER NOT NULL,
            notified_revision INTEGER NOT NULL DEFAULT 0, absent_scans INTEGER NOT NULL DEFAULT 0)""")
        // Verdicts this app queued; such a revision is never revived by a resynced copy.
        db.execSQL("CREATE TABLE submitted (package_id TEXT PRIMARY KEY, revision INTEGER NOT NULL)")
        db.execSQL("""CREATE TABLE alerts (
            alert_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, thread_id TEXT NOT NULL,
            message TEXT NOT NULL, created_at TEXT NOT NULL, state TEXT NOT NULL,
            delete_pending INTEGER NOT NULL DEFAULT 0, first_seen INTEGER NOT NULL,
            changed_at INTEGER NOT NULL, notified INTEGER NOT NULL DEFAULT 0)""")
    }
    /** Older lists described another contract and are not migrated: the list starts empty. */
    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        listOf("proposals", "submitted", "alerts").forEach { db.execSQL("DROP TABLE IF EXISTS $it") }
        onCreate(db)
    }

    data class ReviewItem(
        val packageId: String, val revision: Long, val summary: String, val stage: String,
        val revised: Boolean, val notifiedRevision: Long,
    )
    data class AlertItem(
        val alertId: String, val packageId: String, val threadId: String,
        val message: String, val createdAt: String, val notified: Boolean,
    )

    internal fun setting(key: String): String? = readableDatabase.rawQuery(
        "SELECT value FROM settings WHERE key=?", arrayOf(key),
    ).use { if (it.moveToFirst()) it.getString(0) else null }
    internal fun setting(key: String, value: String) {
        writableDatabase.insertWithOnConflict("settings", null, ContentValues().apply {
            put("key", key); put("value", value)
        }, SQLiteDatabase.CONFLICT_REPLACE)
    }

    /**
     * Apply one complete folder listing from [source] (the selected tree). Returns alert file names to delete.
     *
     * The listing is *trusted* only when [source] equals the previous run's tree and proposals/ was not just
     * created ([rebuilt]). An untrusted listing only adds or updates (a different or new folder proves nothing
     * about absence). On a trusted listing:
     * - an absent package leaves the list (GONE) and counts consecutive absent scans; alerts of a package
     *   that reaches [RESOLVE_AFTER] absent scans in this run are RESOLVED (a feedback revision can briefly
     *   show neither revision);
     * - an absent alert becomes ABSENT (hidden, never deleted) and returns to ACTIVE when its file reappears.
     * A file that is present but [unreadable] counts as present. A revision never moves backwards; a GONE
     * package reappearing returns silently unless this app submitted a verdict for that revision.
     */
    fun reconcile(packages: List<ReviewPackage>, alerts: List<Alert>, unreadable: Set<String>, now: Long,
                  source: String, rebuilt: Boolean): List<String> {
        val db = writableDatabase
        db.beginTransaction()
        try {
            val trusted = setting("tree") == source && !rebuilt
            val heldPackages = unreadable.mapNotNull { CourierRules.revision.matchEntire(it)?.groupValues?.get(1) }.toSet()
            val heldAlerts = unreadable.mapNotNull { CourierRules.alert.matchEntire(it)?.groupValues?.get(1) }.toSet()
            for (p in packages) {
                val old = db.rawQuery("SELECT revision, state FROM reviews WHERE package_id=?", arrayOf(p.packageId))
                    .use { if (it.moveToFirst()) it.getLong(0) to it.getString(1) else null }
                val submitted = db.rawQuery("SELECT revision FROM submitted WHERE package_id=?", arrayOf(p.packageId))
                    .use { if (it.moveToFirst()) it.getLong(0) else 0L }
                val values = ContentValues().apply {
                    put("revision", p.revision); put("summary", p.draftBody.lineSequence().first().take(80))
                    put("stage", p.stage.name); put("state", ACTIVE); put("changed_at", now); put("absent_scans", 0)
                }
                when {
                    old == null -> db.insertOrThrow("reviews", null, values.apply {
                        put("package_id", p.packageId); put("revised", 0); put("first_seen", now)
                    })
                    p.revision > old.first -> db.update("reviews", values.apply { put("revised", 1) },
                        "package_id=?", arrayOf(p.packageId))
                    p.revision == old.first && (old.second == ACTIVE || submitted < p.revision) ->
                        db.update("reviews", ContentValues().apply {
                            put("state", ACTIVE); put("absent_scans", 0); put("stage", p.stage.name)
                        }, "package_id=?", arrayOf(p.packageId))
                    // Older revision resynced, or one this app already decided: keep the row as is.
                }
            }
            for (a in alerts) {
                db.insertWithOnConflict("alerts", null, ContentValues().apply {
                    put("alert_id", a.alertId); put("package_id", a.packageId); put("thread_id", a.threadId)
                    put("message", a.message); put("created_at", a.createdAt); put("state", ACTIVE)
                    put("first_seen", now); put("changed_at", now)
                }, SQLiteDatabase.CONFLICT_IGNORE)
            }
            val alertFiles = alerts.map { it.alertId }.toSet() + heldAlerts
            ids(db, "SELECT alert_id FROM alerts WHERE state='$ABSENT'").filter { it in alertFiles }.forEach {
                db.update("alerts", ContentValues().apply { put("state", ACTIVE); put("changed_at", now) },
                    "alert_id=?", arrayOf(it))
            }
            var delete = emptyList<String>()
            if (trusted) {
                val present = packages.map { it.packageId }.toSet() + heldPackages
                db.execSQL("UPDATE reviews SET absent_scans=0 WHERE package_id IN (${marks(present)})", present.toTypedArray())
                val missing = ids(db, "SELECT package_id FROM reviews").filter { it !in present }
                missing.forEach {
                    db.execSQL("UPDATE reviews SET absent_scans=absent_scans+1, changed_at=? WHERE package_id=?",
                        arrayOf<Any>(now, it))
                    db.execSQL("UPDATE reviews SET state='$GONE' WHERE package_id=? AND state='$ACTIVE'", arrayOf(it))
                }
                // Only packages crossing the threshold in this run resolve alerts; long-gone ones never do.
                val vanished = ids(db, "SELECT package_id FROM reviews WHERE absent_scans=$RESOLVE_AFTER")
                    .filter { it in missing }
                ids(db, "SELECT alert_id FROM alerts WHERE state='$ACTIVE' AND package_id IN (${marks(vanished)})",
                    vanished.toTypedArray()).filter { it in alertFiles }.forEach {
                    db.update("alerts", ContentValues().apply {
                        put("state", RESOLVED); put("delete_pending", 1); put("changed_at", now)
                    }, "alert_id=?", arrayOf(it))
                }
                ids(db, "SELECT alert_id FROM alerts WHERE state='$ACTIVE'").filter { it !in alertFiles }.forEach {
                    db.update("alerts", ContentValues().apply { put("state", ABSENT); put("changed_at", now) },
                        "alert_id=?", arrayOf(it))
                }
                // A RESOLVED alert is deleted only while its resolving listing still shows it. A DISMISSED
                // alert stays pending: the user closed that ID, so its file is deleted wherever it is listed.
                ids(db, "SELECT alert_id FROM alerts WHERE delete_pending=1 AND state='$RESOLVED'")
                    .filter { it !in alertFiles }.forEach {
                        db.execSQL("UPDATE alerts SET delete_pending=0 WHERE alert_id=?", arrayOf(it))
                    }
                delete = ids(db, "SELECT alert_id FROM alerts WHERE delete_pending=1")
                    .filter { it in alertFiles }.map { "alert_$it.json" }
            }
            setting("tree", source)
            setting("last_scan_at", now.toString())
            db.setTransactionSuccessful()
            return delete
        } finally { db.endTransaction() }
    }

    /** Record successful deletions so a resynced copy of a resolved alert is not deleted again. */
    fun deleted(names: List<String>) {
        names.forEach { name ->
            CourierRules.alert.matchEntire(name)?.groupValues?.get(1)?.let {
                writableDatabase.execSQL("UPDATE alerts SET delete_pending=0 WHERE alert_id=? AND state='$RESOLVED'",
                    arrayOf(it))
            }
        }
    }

    /** This app queued a verdict for [revision]; that revision is never revived. */
    fun submitted(packageId: String, revision: Long) {
        writableDatabase.execSQL("""INSERT INTO submitted(package_id, revision) VALUES(?, ?)
            ON CONFLICT(package_id) DO UPDATE SET revision=MAX(revision, excluded.revision)""", arrayOf<Any>(packageId, revision))
    }

    /** The user closes an alert; its file is deleted on the next trusted scan that lists it. */
    fun dismiss(alertId: String, now: Long): Boolean = writableDatabase.update("alerts",
        ContentValues().apply { put("state", DISMISSED); put("delete_pending", 1); put("changed_at", now) },
        "alert_id=? AND state='$ACTIVE'", arrayOf(alertId)) == 1

    fun reviews(newestRevisionFirst: Boolean = false): List<ReviewItem> = readableDatabase.rawQuery(
        """SELECT package_id, revision, summary, stage, revised, notified_revision FROM reviews
            WHERE state='$ACTIVE' ORDER BY ${if (newestRevisionFirst) "changed_at" else "first_seen"} DESC, package_id DESC""", null,
    ).use { c -> buildList {
        while (c.moveToNext()) add(ReviewItem(c.getString(0), c.getLong(1), c.getString(2), c.getString(3),
            c.getInt(4) == 1, c.getLong(5)))
    } }

    fun alerts(): List<AlertItem> = readableDatabase.rawQuery(
        """SELECT alert_id, package_id, thread_id, message, created_at, notified FROM alerts
            WHERE state='$ACTIVE' ORDER BY first_seen DESC, alert_id DESC""", null,
    ).use { c -> buildList {
        while (c.moveToNext()) add(AlertItem(c.getString(0), c.getString(1), c.getString(2), c.getString(3),
            c.getString(4), c.getInt(5) == 1))
    } }

    internal fun markReviewsNotified() {
        writableDatabase.execSQL("UPDATE reviews SET notified_revision=revision WHERE state='$ACTIVE'")
    }
    internal fun markAlertsNotified() {
        writableDatabase.execSQL("UPDATE alerts SET notified=1 WHERE state='$ACTIVE'")
    }

    fun lastScanAt(): Long? = setting("last_scan_at")?.toLongOrNull()

    private fun ids(db: SQLiteDatabase, sql: String, args: Array<String>? = null): List<String> =
        db.rawQuery(sql, args).use { c -> buildList { while (c.moveToNext()) add(c.getString(0)) } }
    private fun marks(values: Collection<String>) = values.joinToString(",") { "?" }

    companion object {
        const val ACTIVE = "ACTIVE"
        const val GONE = "GONE"
        const val DISMISSED = "DISMISSED"
        const val RESOLVED = "RESOLVED"
        const val ABSENT = "ABSENT"
        /** Consecutive trusted scans without the evidence package before its alerts resolve. */
        const val RESOLVE_AFTER = 2
    }
}
