package io.github.junhcha.weaver.inbox

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import io.github.junhcha.weaver.courier.CourierRules
import io.github.junhcha.weaver.schema.Alert
import io.github.junhcha.weaver.schema.Proposal
import io.github.junhcha.weaver.schema.v2.ReviewPackage
import java.io.File

/**
 * Local list of proposals and change alerts shared by notifications and the widget (#34).
 * Rows are never deleted: their states are the per-ID notification history that keeps resynced
 * or restarted scans from notifying twice. Only summary/message are stored; thread excerpts and
 * full proposals stay in the Courier folder for the review screen (#35).
 *
 * Deleting an alert file is irreversible (Syncthing propagates it to the PC), so it happens only for
 * DISMISSED alerts and for alerts RESOLVED in this very run, and only on a trusted listing.
 */
class InboxDatabase(context: Context) : SQLiteOpenHelper(
    context, File(context.filesDir, "inbox.sqlite3").absolutePath, null, 2,
), java.io.Closeable {
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execSQL("""CREATE TABLE proposals (
            proposal_id TEXT PRIMARY KEY, rev INTEGER NOT NULL, summary TEXT NOT NULL,
            channel TEXT NOT NULL, captured_at TEXT NOT NULL, revised INTEGER NOT NULL,
            state TEXT NOT NULL, first_seen INTEGER NOT NULL, changed_at INTEGER NOT NULL,
            notified_rev INTEGER NOT NULL DEFAULT 0, absent_scans INTEGER NOT NULL DEFAULT 0, schema_version INTEGER NOT NULL DEFAULT 1, stage TEXT)""")
        // Verdicts this app queued; such a proposal rev is never revived by a resynced copy.
        db.execSQL("CREATE TABLE submitted (proposal_id TEXT PRIMARY KEY, rev INTEGER NOT NULL)")
        db.execSQL("""CREATE TABLE alerts (
            alert_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, schedule_ref TEXT NOT NULL,
            message TEXT NOT NULL, created_at TEXT NOT NULL, state TEXT NOT NULL,
            delete_pending INTEGER NOT NULL DEFAULT 0, first_seen INTEGER NOT NULL,
            changed_at INTEGER NOT NULL, notified INTEGER NOT NULL DEFAULT 0)""")
    }
    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        if (oldVersion < 2) {
            db.execSQL("ALTER TABLE proposals ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1")
            db.execSQL("ALTER TABLE proposals ADD COLUMN stage TEXT")
        }
    }

    data class ProposalItem(
        val proposalId: String, val rev: Long, val summary: String, val channel: String,
        val capturedAt: String, val revised: Boolean, val notifiedRev: Long,
        val isV2: Boolean = false, val stage: String? = null,
    )
    data class AlertItem(
        val alertId: String, val proposalId: String, val scheduleRef: String,
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
     * - an absent proposal leaves the list (GONE) and counts consecutive absent scans; alerts of a proposal
     *   that reaches [RESOLVE_AFTER] absent scans in this run are RESOLVED (a revise can briefly show neither rev);
     * - an absent alert becomes ABSENT (hidden, never deleted) and returns to ACTIVE when its file reappears.
     * A file that is present but [unreadable] counts as present. A rev never moves backwards; a GONE proposal
     * reappearing returns silently unless this app submitted a verdict for that rev.
     */
    fun reconcile(proposals: List<Proposal>, alerts: List<Alert>, unreadable: Set<String>, now: Long,
                  source: String, rebuilt: Boolean, packages: List<ReviewPackage> = emptyList()): List<String> {
        val db = writableDatabase
        db.beginTransaction()
        try {
            val trusted = setting("tree") == source && !rebuilt
            val heldProposals = unreadable.mapNotNull { CourierRules.revision.matchEntire(it)?.groupValues?.get(1) }.toSet()
            val heldAlerts = unreadable.mapNotNull { CourierRules.alert.matchEntire(it)?.groupValues?.get(1) }.toSet()
            data class Entry(val id: String, val rev: Long, val summary: String, val channel: String,
                             val capturedAt: String, val version: Int, val stage: String?)
            val entries = proposals.map { Entry(it.proposalId, it.rev, it.summary, it.source.channel.name, it.source.capturedAt, 1, null) } +
                packages.map { Entry(it.packageId, it.revision, it.draftBody.lineSequence().first().take(80), "", "", 2, it.stage.name) }
            for (p in entries) {
                val old = db.rawQuery("SELECT rev, state FROM proposals WHERE proposal_id=?", arrayOf(p.id))
                    .use { if (it.moveToFirst()) it.getLong(0) to it.getString(1) else null }
                val submitted = db.rawQuery("SELECT rev FROM submitted WHERE proposal_id=?", arrayOf(p.id))
                    .use { if (it.moveToFirst()) it.getLong(0) else 0L }
                val values = ContentValues().apply {
                    put("rev", p.rev); put("summary", p.summary); put("channel", p.channel)
                    put("captured_at", p.capturedAt); put("state", ACTIVE); put("changed_at", now)
                    put("absent_scans", 0); put("schema_version", p.version); put("stage", p.stage)
                }
                when {
                    old == null -> db.insertOrThrow("proposals", null, values.apply {
                        put("proposal_id", p.id); put("revised", 0); put("first_seen", now)
                    })
                    p.rev > old.first -> db.update("proposals", values.apply { put("revised", 1) },
                        "proposal_id=?", arrayOf(p.id))
                    p.rev == old.first && (old.second == ACTIVE || submitted < p.rev) ->
                        db.update("proposals", ContentValues().apply {
                            put("state", ACTIVE); put("absent_scans", 0); put("stage", p.stage)
                        }, "proposal_id=?", arrayOf(p.id))
                    // Older rev resynced, or a rev this app already decided: keep the row as is.
                }
            }
            for (a in alerts) {
                db.insertWithOnConflict("alerts", null, ContentValues().apply {
                    put("alert_id", a.alertId); put("proposal_id", a.proposalId); put("schedule_ref", a.scheduleRef)
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
                val present = entries.map { it.id }.toSet() + heldProposals
                db.execSQL("UPDATE proposals SET absent_scans=0 WHERE proposal_id IN (${marks(present)})", present.toTypedArray())
                val missing = ids(db, "SELECT proposal_id FROM proposals").filter { it !in present }
                missing.forEach {
                    db.execSQL("UPDATE proposals SET absent_scans=absent_scans+1, changed_at=? WHERE proposal_id=?",
                        arrayOf<Any>(now, it))
                    db.execSQL("UPDATE proposals SET state='$GONE' WHERE proposal_id=? AND state='$ACTIVE'", arrayOf(it))
                }
                // Only proposals crossing the threshold in this run resolve alerts; long-gone ones never do.
                val vanished = ids(db, "SELECT proposal_id FROM proposals WHERE absent_scans=$RESOLVE_AFTER")
                    .filter { it in missing }
                ids(db, "SELECT alert_id FROM alerts WHERE state='$ACTIVE' AND proposal_id IN (${marks(vanished)})",
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

    /** This app queued a verdict for [rev]; that rev is never revived. */
    fun submitted(proposalId: String, rev: Long) {
        writableDatabase.execSQL("""INSERT INTO submitted(proposal_id, rev) VALUES(?, ?)
            ON CONFLICT(proposal_id) DO UPDATE SET rev=MAX(rev, excluded.rev)""", arrayOf<Any>(proposalId, rev))
    }

    /** The user closes an alert; its file is deleted on the next trusted scan that lists it. */
    fun dismiss(alertId: String, now: Long): Boolean = writableDatabase.update("alerts",
        ContentValues().apply { put("state", DISMISSED); put("delete_pending", 1); put("changed_at", now) },
        "alert_id=? AND state='$ACTIVE'", arrayOf(alertId)) == 1

    fun proposals(newestRevisionFirst: Boolean = false): List<ProposalItem> = readableDatabase.rawQuery(
        """SELECT proposal_id, rev, summary, channel, captured_at, revised, notified_rev, schema_version, stage FROM proposals
            WHERE state='$ACTIVE' ORDER BY ${if (newestRevisionFirst) "changed_at" else "first_seen"} DESC, proposal_id DESC""", null,
    ).use { c -> buildList {
        while (c.moveToNext()) add(ProposalItem(c.getString(0), c.getLong(1), c.getString(2), c.getString(3),
            c.getString(4), c.getInt(5) == 1, c.getLong(6), c.getInt(7) == 2, c.getString(8)))
    } }

    fun alerts(): List<AlertItem> = readableDatabase.rawQuery(
        """SELECT alert_id, proposal_id, schedule_ref, message, created_at, notified FROM alerts
            WHERE state='$ACTIVE' ORDER BY first_seen DESC, alert_id DESC""", null,
    ).use { c -> buildList {
        while (c.moveToNext()) add(AlertItem(c.getString(0), c.getString(1), c.getString(2), c.getString(3),
            c.getString(4), c.getInt(5) == 1))
    } }

    internal fun markProposalsNotified() {
        writableDatabase.execSQL("UPDATE proposals SET notified_rev=rev WHERE state='$ACTIVE'")
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
        /** Consecutive trusted scans without the evidence proposal before its alerts resolve. */
        const val RESOLVE_AFTER = 2
    }
}
