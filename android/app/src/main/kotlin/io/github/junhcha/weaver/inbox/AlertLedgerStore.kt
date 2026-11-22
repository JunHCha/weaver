package io.github.junhcha.weaver.inbox

import android.content.ContentValues
import android.database.sqlite.SQLiteDatabase
import io.github.junhcha.weaver.schema.Alert
import io.github.junhcha.weaver.schema.WeaverCodec

/** Phone-owned receipt, notification and local read/close history. Adapter absence changes none of it. */
internal class AlertLedgerStore(private val db: SQLiteDatabase) {
    fun create() {
        db.execSQL("""CREATE TABLE alert_ledger (
            server_id TEXT NOT NULL, alert_id TEXT NOT NULL, body TEXT NOT NULL,
            received_at INTEGER NOT NULL, posted INTEGER NOT NULL DEFAULT 0,
            read_at INTEGER, closed_at INTEGER, received_via TEXT NOT NULL DEFAULT 'files', PRIMARY KEY(server_id, alert_id))""")
        createHttpReceipts()
        db.execSQL("CREATE TABLE alert_sources (source TEXT PRIMARY KEY, server_id TEXT NOT NULL)")
        db.execSQL("""CREATE TABLE alert_receipts (server_id TEXT NOT NULL, device_id TEXT NOT NULL,
            alert_id TEXT NOT NULL, PRIMARY KEY(server_id, device_id, alert_id))""")
    }

    fun createHttpReceipts() {
        db.execSQL("""CREATE TABLE alert_http_received (server_id TEXT NOT NULL, device_id TEXT NOT NULL,
            alert_id TEXT NOT NULL, PRIMARY KEY(server_id, device_id, alert_id))""")
    }

    private fun decoded(body: String): Alert = io.github.junhcha.weaver.courier.CourierClient.decoded(body)

    fun mapped(source: String): String? = db.rawQuery(
        "SELECT server_id FROM alert_sources WHERE source=?", arrayOf(source),
    ).use { if (it.moveToFirst()) it.getString(0) else null }

    /** Fixed association; re-pairing cannot quietly send another servers SAF IDs to this server. */
    fun bind(source: String, serverId: String): String {
        val old = mapped(source)
        check(old == null || old == serverId || old == "files:$source") { "alert_source_server_conflict" }
        if (old == "files:$source" && old != serverId) {
            val conflict = db.rawQuery("""SELECT a.body, b.body FROM alert_ledger a JOIN alert_ledger b ON a.alert_id=b.alert_id
                WHERE a.server_id=? AND b.server_id=? AND a.body!='' AND b.body!=''""", arrayOf(old, serverId))
                .use { c -> var differs = false; while (c.moveToNext()) if (decoded(c.getString(0)) != decoded(c.getString(1))) differs = true; differs }
            check(!conflict) { "alert_body_conflict" }
            // Preserve any HTTP history; merge local close/read/post history, never resurrect it.
            db.execSQL("""UPDATE alert_ledger SET
                posted=MAX(posted, COALESCE((SELECT posted FROM alert_ledger old WHERE old.server_id=? AND old.alert_id=alert_ledger.alert_id), 0)),
                read_at=COALESCE(read_at, (SELECT read_at FROM alert_ledger old WHERE old.server_id=? AND old.alert_id=alert_ledger.alert_id)),
                closed_at=COALESCE(closed_at, (SELECT closed_at FROM alert_ledger old WHERE old.server_id=? AND old.alert_id=alert_ledger.alert_id))
                WHERE server_id=?""", arrayOf(old, old, old, serverId))
            db.execSQL("INSERT OR IGNORE INTO alert_ledger(server_id, alert_id, body, received_at, posted, read_at, closed_at, received_via) SELECT ?, alert_id, body, received_at, posted, read_at, closed_at, received_via FROM alert_ledger WHERE server_id=?",
                arrayOf(serverId, old))
            db.execSQL("DELETE FROM alert_ledger WHERE server_id=?", arrayOf(old))
        }
        db.insertWithOnConflict("alert_sources", null, ContentValues().apply {
            put("source", source); put("server_id", serverId)
        }, SQLiteDatabase.CONFLICT_REPLACE)
        return serverId
    }

    fun receive(serverId: String, alert: Alert, now: Long, deviceId: String? = null) {
        val body = WeaverCodec.encode(alert)
        val old = db.rawQuery("SELECT body FROM alert_ledger WHERE server_id=? AND alert_id=?",
            arrayOf(serverId, alert.alertId)).use { if (it.moveToFirst()) it.getString(0) else null }
        check(old == null || old.isEmpty() || decoded(old) == alert) { "alert_body_conflict" }
        db.insertWithOnConflict("alert_ledger", null, ContentValues().apply {
            put("server_id", serverId); put("alert_id", alert.alertId); put("body", body); put("received_at", now)
            put("received_via", if (deviceId == null) "files" else "http")
        }, SQLiteDatabase.CONFLICT_IGNORE)
        if (deviceId != null) {
            db.execSQL("UPDATE alert_ledger SET received_via='http' WHERE server_id=? AND alert_id=?", arrayOf(serverId, alert.alertId))
            db.execSQL("INSERT OR IGNORE INTO alert_http_received VALUES(?, ?, ?)", arrayOf(serverId, deviceId, alert.alertId))
        }
    }

    fun bodies(serverId: String, activeOnly: Boolean = true): List<Alert> = db.rawQuery(
        "SELECT body FROM alert_ledger WHERE server_id=? AND body!='' ${if (activeOnly) "AND closed_at IS NULL" else ""} ORDER BY received_at DESC, alert_id DESC",
        arrayOf(serverId),
    ).use { c -> buildList { while (c.moveToNext()) add(io.github.junhcha.weaver.courier.CourierClient.decoded<Alert>(c.getString(0))) } }

    /** Body and post history share one query; the list screen does not read SQLite per row. */
    fun items(serverId: String): List<InboxDatabase.AlertItem> = db.rawQuery(
        "SELECT body, posted FROM alert_ledger WHERE server_id=? AND closed_at IS NULL ORDER BY received_at DESC, alert_id DESC", arrayOf(serverId),
    ).use { c -> buildList { while (c.moveToNext()) {
        val a = decoded(c.getString(0))
        add(InboxDatabase.AlertItem(a.alertId, a.packageId, a.threadId, a.message, a.createdAt, c.getInt(1) == 1, serverId))
    } } }

    /** Retain the ID/close tombstone so resynced files cannot resurrect a purged body. */
    fun pruneClosed(now: Long) {
        db.execSQL("UPDATE alert_ledger SET body='' WHERE closed_at IS NOT NULL AND closed_at<=?", arrayOf(now - 30L * 86_400_000L))
    }

    fun closedIds(serverId: String): Set<String> = db.rawQuery(
        "SELECT alert_id FROM alert_ledger WHERE server_id=? AND closed_at IS NOT NULL", arrayOf(serverId),
    ).use { c -> buildSet { while (c.moveToNext()) add(c.getString(0)) } }

    fun pending(serverId: String, deviceId: String): List<String> = db.rawQuery(
        """SELECT alert_id FROM alert_ledger a WHERE server_id=? AND EXISTS
            (SELECT 1 FROM alert_http_received h WHERE h.server_id=a.server_id AND h.alert_id=a.alert_id AND h.device_id=?) AND NOT EXISTS
            (SELECT 1 FROM alert_receipts r WHERE r.server_id=a.server_id AND r.alert_id=a.alert_id AND r.device_id=?)""",
        arrayOf(serverId, deviceId, deviceId),
    ).use { c -> buildList { while (c.moveToNext()) add(c.getString(0)) } }

    fun acknowledged(serverId: String, deviceId: String, alertId: String) {
        db.execSQL("INSERT OR IGNORE INTO alert_receipts VALUES(?, ?, ?)", arrayOf(serverId, deviceId, alertId))
    }

    fun close(serverId: String, alertId: String, now: Long): Boolean = db.update("alert_ledger", ContentValues().apply {
        put("closed_at", now); put("read_at", now)
    }, "server_id=? AND alert_id=? AND closed_at IS NULL", arrayOf(serverId, alertId)) == 1
}
