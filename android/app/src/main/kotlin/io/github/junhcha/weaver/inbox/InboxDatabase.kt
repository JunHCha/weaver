package io.github.junhcha.weaver.inbox

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import io.github.junhcha.weaver.courier.CourierRules
import io.github.junhcha.weaver.schema.Alert
import io.github.junhcha.weaver.schema.Proposal
import java.io.File

/**
 * Local list of proposals and change alerts shared by notifications and the widget (#34).
 * Rows are never deleted: GONE/DISMISSED/RESOLVED rows are the per-ID notification history that
 * keeps resynced or restarted scans from notifying twice. Only summary/message are stored;
 * thread excerpts and full proposals stay in the Courier folder for the review screen (#35).
 */
class InboxDatabase(context: Context) : SQLiteOpenHelper(
    context, File(context.filesDir, "inbox.sqlite3").absolutePath, null, 1,
), java.io.Closeable {
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execSQL("""CREATE TABLE proposals (
            proposal_id TEXT PRIMARY KEY, rev INTEGER NOT NULL, summary TEXT NOT NULL,
            channel TEXT NOT NULL, captured_at TEXT NOT NULL, revised INTEGER NOT NULL,
            state TEXT NOT NULL, first_seen INTEGER NOT NULL, changed_at INTEGER NOT NULL,
            notified_rev INTEGER NOT NULL DEFAULT 0)""")
        db.execSQL("""CREATE TABLE alerts (
            alert_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, schedule_ref TEXT NOT NULL,
            message TEXT NOT NULL, created_at TEXT NOT NULL, state TEXT NOT NULL,
            proposal_seen INTEGER NOT NULL DEFAULT 0, first_seen INTEGER NOT NULL,
            changed_at INTEGER NOT NULL, notified INTEGER NOT NULL DEFAULT 0)""")
    }
    // Before the next schema version, add a migration and a test that opens a v1 database.
    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit

    data class ProposalItem(
        val proposalId: String, val rev: Long, val summary: String, val channel: String,
        val capturedAt: String, val revised: Boolean, val notifiedRev: Long,
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
     * Apply one complete, successful folder listing. Returns alert file names to delete
     * (dismissed or resolved alerts whose file is still present). A file that is present but
     * [unreadable] keeps its previous state; a proposal rev never moves backwards and a GONE
     * proposal is not revived by a resynced copy of the same or an older rev.
     */
    fun reconcile(proposals: List<Proposal>, alerts: List<Alert>, unreadable: Set<String>, now: Long): List<String> {
        val db = writableDatabase
        db.beginTransaction()
        try {
            val heldProposals = unreadable.mapNotNull { CourierRules.revision.matchEntire(it)?.groupValues?.get(1) }.toSet()
            val heldAlerts = unreadable.mapNotNull { CourierRules.alert.matchEntire(it)?.groupValues?.get(1) }.toSet()
            for (p in proposals) {
                val old = db.rawQuery("SELECT rev FROM proposals WHERE proposal_id=?", arrayOf(p.proposalId))
                    .use { if (it.moveToFirst()) it.getLong(0) else null }
                val values = ContentValues().apply {
                    put("rev", p.rev); put("summary", p.summary); put("channel", p.source.channel.name)
                    put("captured_at", p.source.capturedAt); put("state", ACTIVE); put("changed_at", now)
                }
                when {
                    old == null -> db.insertOrThrow("proposals", null, values.apply {
                        put("proposal_id", p.proposalId); put("revised", 0); put("first_seen", now)
                    })
                    p.rev > old -> db.update("proposals", values.apply { put("revised", 1) },
                        "proposal_id=?", arrayOf(p.proposalId))
                }
            }
            val present = proposals.map { it.proposalId }.toSet() + heldProposals
            ids(db, "SELECT proposal_id FROM proposals WHERE state='$ACTIVE'").filter { it !in present }.forEach {
                db.update("proposals", ContentValues().apply { put("state", GONE); put("changed_at", now) },
                    "proposal_id=?", arrayOf(it))
            }

            for (a in alerts) {
                db.insertWithOnConflict("alerts", null, ContentValues().apply {
                    put("alert_id", a.alertId); put("proposal_id", a.proposalId); put("schedule_ref", a.scheduleRef)
                    put("message", a.message); put("created_at", a.createdAt); put("state", ACTIVE)
                    put("first_seen", now); put("changed_at", now)
                }, SQLiteDatabase.CONFLICT_IGNORE)
            }
            // Any local row (active or gone) means the evidence proposal was seen on this phone.
            db.execSQL("""UPDATE alerts SET proposal_seen=1 WHERE state='$ACTIVE'
                AND proposal_id IN (SELECT proposal_id FROM proposals)""")
            db.execSQL("""UPDATE alerts SET state='$RESOLVED', changed_at=$now WHERE state='$ACTIVE' AND proposal_seen=1
                AND proposal_id IN (SELECT proposal_id FROM proposals WHERE state='$GONE')""")
            val alertFiles = alerts.map { it.alertId }.toSet() + heldAlerts
            // Removed by someone else (e.g. a manual cleanup): nothing left to confirm.
            ids(db, "SELECT alert_id FROM alerts WHERE state='$ACTIVE'").filter { it !in alertFiles }.forEach {
                db.update("alerts", ContentValues().apply { put("state", RESOLVED); put("changed_at", now) },
                    "alert_id=?", arrayOf(it))
            }
            val delete = ids(db, "SELECT alert_id FROM alerts WHERE state!='$ACTIVE'")
                .filter { it in alertFiles }.map { "alert_$it.json" }
            setting("last_scan_at", now.toString())
            db.setTransactionSuccessful()
            return delete
        } finally { db.endTransaction() }
    }

    /** The user closes an alert. Returns false for unknown or already closed alerts. */
    fun dismiss(alertId: String, now: Long): Boolean = writableDatabase.update("alerts",
        ContentValues().apply { put("state", DISMISSED); put("changed_at", now) },
        "alert_id=? AND state='$ACTIVE'", arrayOf(alertId)) == 1

    fun proposals(): List<ProposalItem> = readableDatabase.rawQuery(
        """SELECT proposal_id, rev, summary, channel, captured_at, revised, notified_rev FROM proposals
            WHERE state='$ACTIVE' ORDER BY first_seen DESC, proposal_id DESC""", null,
    ).use { c -> buildList {
        while (c.moveToNext()) add(ProposalItem(c.getString(0), c.getLong(1), c.getString(2), c.getString(3),
            c.getString(4), c.getInt(5) == 1, c.getLong(6)))
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

    private fun ids(db: SQLiteDatabase, sql: String): List<String> = db.rawQuery(sql, null).use { c ->
        buildList { while (c.moveToNext()) add(c.getString(0)) }
    }

    companion object {
        const val ACTIVE = "ACTIVE"
        const val GONE = "GONE"
        const val DISMISSED = "DISMISSED"
        const val RESOLVED = "RESOLVED"
    }
}
