package io.github.junhcha.weaver.inbox

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import io.github.junhcha.weaver.courier.CourierRules
import io.github.junhcha.weaver.schema.Alert
import io.github.junhcha.weaver.schema.ReviewPackage
import java.io.File

/** Review index and durable phone-owned Alert ledger; synchronous calls belong on an IO thread. */
class InboxDatabase(context: Context) : SQLiteOpenHelper(
    context, File(context.filesDir, "inbox.sqlite3").absolutePath, null, 5,
), java.io.Closeable {
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execSQL("""CREATE TABLE reviews (
            package_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, summary TEXT NOT NULL,
            stage TEXT NOT NULL, revised INTEGER NOT NULL, state TEXT NOT NULL,
            first_seen INTEGER NOT NULL, changed_at INTEGER NOT NULL,
            notified_revision INTEGER NOT NULL DEFAULT 0, absent_scans INTEGER NOT NULL DEFAULT 0)""")
        AlertLedgerStore(db).create()
        // Verdicts this app queued; such a revision is never revived by a resynced copy.
        db.execSQL("CREATE TABLE submitted (package_id TEXT PRIMARY KEY, revision INTEGER NOT NULL)")
        db.execSQL("""CREATE TABLE alerts (
            alert_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, thread_id TEXT NOT NULL,
            message TEXT NOT NULL, created_at TEXT NOT NULL, state TEXT NOT NULL,
            delete_pending INTEGER NOT NULL DEFAULT 0, first_seen INTEGER NOT NULL,
            changed_at INTEGER NOT NULL, notified INTEGER NOT NULL DEFAULT 0)""")
    }
    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        if (oldVersion < 3) {
            listOf("proposals", "reviews", "submitted", "alerts").forEach { db.execSQL("DROP TABLE IF EXISTS $it") }
            onCreate(db)
            return
        }
        val ledger = AlertLedgerStore(db)
        if (oldVersion == 4) {
            db.execSQL("ALTER TABLE alert_ledger ADD COLUMN received_via TEXT NOT NULL DEFAULT 'files'")
            ledger.createHttpReceipts()
            return
        }
        ledger.create()
        val source = db.rawQuery("SELECT value FROM settings WHERE key=?", arrayOf("tree")).use { if (it.moveToFirst()) it.getString(0) else "legacy" }
        val server = if (source.startsWith("http:")) source.removePrefix("http:") else "files:$source"
        ledger.bind(source, server)
        db.rawQuery("SELECT alert_id, package_id, thread_id, message, created_at, state, first_seen, notified, changed_at FROM alerts", null).use { c ->
            while (c.moveToNext()) {
                // Keep invalid v3 rows verbatim in alerts for diagnosis without blocking migration.
                val alert = try { Alert(2, c.getString(0), c.getString(1), c.getString(2), c.getString(3), c.getString(4)) }
                catch (_: IllegalArgumentException) { continue }
                ledger.receive(server, alert, c.getLong(6))
                if (c.getString(5) in setOf(DISMISSED, RESOLVED)) ledger.close(server, alert.alertId, c.getLong(8))
                if (c.getInt(7) == 1) db.execSQL("UPDATE alert_ledger SET posted=1 WHERE server_id=? AND alert_id=?", arrayOf(server, alert.alertId))
            }
        }
        db.execSQL("INSERT OR REPLACE INTO settings VALUES(?, ?)", arrayOf("alert_server", server))
    }

    private fun ledger() = AlertLedgerStore(writableDatabase)
    internal fun alertServer(): String = setting("alert_server") ?: "legacy"

    /** Called on IO, in the same transaction as receipt reconciliation when used by a scan. */
    fun selectAlertSource(source: String, serverId: String? = null): String {
        val store = ledger()
        val server = serverId ?: store.mapped(source) ?: if (source.startsWith("http:")) source.removePrefix("http:") else "files:$source"
        store.bind(source, server)
        setting("alert_server", server)
        return server
    }

    fun bindAlertSource(source: String, serverId: String) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            val old = ledger().mapped(source)
            ledger().bind(source, serverId)
            if (old != null && alertServer() == old) setting("alert_server", serverId)
            db.setTransactionSuccessful()
        } finally { db.endTransaction() }
    }

    fun receiveAlert(serverId: String, alert: Alert, now: Long, deviceId: String? = null) {
        val db = writableDatabase
        db.beginTransaction()
        try { ledger().receive(serverId, alert, now, deviceId); db.setTransactionSuccessful() } finally { db.endTransaction() }
    }
    fun pruneClosedAlerts(now: Long) = ledger().pruneClosed(now)
    fun pendingAlertAcks(serverId: String, deviceId: String): List<String> = ledger().pending(serverId, deviceId)
    fun alertAcknowledged(serverId: String, deviceId: String, alertId: String) = ledger().acknowledged(serverId, deviceId, alertId)
    fun alertBodies(serverId: String): List<Alert> = ledger().bodies(serverId)

    fun markAlertRead(serverId: String, alertId: String, now: Long) {
        writableDatabase.execSQL("UPDATE alert_ledger SET read_at=COALESCE(read_at, ?) WHERE server_id=? AND alert_id=?", arrayOf<Any>(now, serverId, alertId))
    }

    data class ReviewItem(
        val packageId: String, val revision: Long, val summary: String, val stage: String,
        val revised: Boolean, val notifiedRevision: Long,
    )
    data class AlertItem(
        val alertId: String, val packageId: String, val threadId: String,
        val message: String, val createdAt: String, val notified: Boolean, val serverId: String = "legacy",
    )

    internal fun setting(key: String): String? = readableDatabase.rawQuery(
        "SELECT value FROM settings WHERE key=?", arrayOf(key),
    ).use { if (it.moveToFirst()) it.getString(0) else null }
    internal fun setting(key: String, value: String) {
        writableDatabase.insertWithOnConflict("settings", null, ContentValues().apply {
            put("key", key); put("value", value)
        }, SQLiteDatabase.CONFLICT_REPLACE)
    }

    /** Reconcile review absence only; Alert absence never closes or removes a cached body. */
    fun reconcile(packages: List<ReviewPackage>, alerts: List<Alert>, unreadable: Set<String>, now: Long,
                  source: String, rebuilt: Boolean, alertSource: String = source, serverId: String? = null, onAlertError: (Exception) -> Unit = { throw it }): List<String> {
        val db = writableDatabase
        db.beginTransaction()
        try {
            val trusted = setting("tree") == source && !rebuilt
            val heldPackages = unreadable.mapNotNull { CourierRules.revision.matchEntire(it)?.groupValues?.get(1) }.toSet()
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
            if (trusted) {
                val present = packages.map { it.packageId }.toSet() + heldPackages
                db.execSQL("UPDATE reviews SET absent_scans=0 WHERE package_id IN (${marks(present)})", present.toTypedArray())
                val missing = ids(db, "SELECT package_id FROM reviews").filter { it !in present }
                missing.forEach {
                    db.execSQL("UPDATE reviews SET absent_scans=absent_scans+1, changed_at=? WHERE package_id=?",
                        arrayOf<Any>(now, it))
                    db.execSQL("UPDATE reviews SET state='$GONE' WHERE package_id=? AND state='$ACTIVE'", arrayOf(it))
                }
            }
            setting("tree", source)
            setting("last_scan_at", now.toString())
            db.setTransactionSuccessful()
        } finally { db.endTransaction() }
        // Alert failures cannot roll back committed review ingestion/history.
        db.beginTransaction()
        try {
            val alertServer = selectAlertSource(alertSource, serverId)
            for (a in alerts) ledger().receive(alertServer, a, now)
            ledger().pruneClosed(now)
            val closed = ledger().closedIds(alertServer)
            val delete = alerts.filter { it.alertId in closed }.map { "alert_${it.alertId}.json" }
            db.setTransactionSuccessful()
            return delete
        } catch (error: Exception) { onAlertError(error); return emptyList()
        } finally { db.endTransaction() }
    }

    /** This app queued a verdict for [revision]; that revision is never revived. */
    fun submitted(packageId: String, revision: Long) {
        writableDatabase.execSQL("""INSERT INTO submitted(package_id, revision) VALUES(?, ?)
            ON CONFLICT(package_id) DO UPDATE SET revision=MAX(revision, excluded.revision)""", arrayOf<Any>(packageId, revision))
    }

    /** Close only locally; the ledger keeps the body and replays adapter cleanup. */
    fun dismiss(alertId: String, now: Long, serverId: String = alertServer()): Boolean = ledger().close(serverId, alertId, now)

    fun reviews(newestRevisionFirst: Boolean = false): List<ReviewItem> = readableDatabase.rawQuery(
        """SELECT package_id, revision, summary, stage, revised, notified_revision FROM reviews
            WHERE state='$ACTIVE' ORDER BY ${if (newestRevisionFirst) "changed_at" else "first_seen"} ${if (newestRevisionFirst) "DESC" else "ASC"}, package_id ASC""", null,
    ).use { c -> buildList {
        while (c.moveToNext()) add(ReviewItem(c.getString(0), c.getLong(1), c.getString(2), c.getString(3),
            c.getInt(4) == 1, c.getLong(5)))
    } }

    fun alerts(): List<AlertItem> = ledger().items(alertServer())

    internal fun markReviewsNotified() {
        writableDatabase.execSQL("UPDATE reviews SET notified_revision=revision WHERE state='$ACTIVE'")
    }
    internal fun prepareAlertsNotification(signature: String) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            setting("alert_pending", signature)
            db.setTransactionSuccessful()
        } finally { db.endTransaction() }
    }

    internal fun markAlertsNotified() {
        writableDatabase.execSQL("UPDATE alert_ledger SET posted=1 WHERE server_id=? AND closed_at IS NULL", arrayOf(alertServer()))
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
    }
}
