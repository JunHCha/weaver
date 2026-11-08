package io.github.junhcha.weaver.shepherd

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import java.time.ZoneId

/** Metadata only. Neither this store nor its scanner opens an audio stream. */
class ShepherdDatabase(context: Context) : SQLiteOpenHelper(context, "shepherd.sqlite3", null, 1), ShepherdCalls, java.io.Closeable {
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execSQL("""CREATE TABLE calls (id TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL,
            modified INTEGER NOT NULL, stable_since INTEGER NOT NULL, zone TEXT NOT NULL,
            counterpart TEXT, occurred_at TEXT, state TEXT NOT NULL)""")
    }
    // Before the next schema version, add a migration and tests opening the previous-version DB
    // and verifying that activation, observation, processing and notification settings survive.
    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit
    internal fun setting(key: String): String? = readableDatabase.rawQuery(
        "SELECT value FROM settings WHERE key=?", arrayOf(key),
    ).use { if (it.moveToFirst()) it.getString(0) else null }
    internal fun setting(key: String, value: String) {
        writableDatabase.insertWithOnConflict("settings", null, ContentValues().apply {
            put("key", key); put("value", value)
        }, SQLiteDatabase.CONFLICT_REPLACE)
    }
    fun activate(at: Long) {
        writableDatabase.insertWithOnConflict("settings", null, ContentValues().apply {
            put("key", "activated_at"); put("value", at.toString())
        }, SQLiteDatabase.CONFLICT_IGNORE)
    }
    internal fun activatedAt(): Long? = setting("activated_at")?.toLong()

    /** Return whether another observation is needed. Changes reset the full 30-second window. */
    internal fun observe(rows: List<RecordingMetadata>, now: Long, zone: ZoneId): Boolean {
        val activated = activatedAt() ?: return false
        val db = writableDatabase
        db.beginTransaction()
        try {
            val present = rows.map { it.uri }.toSet()
            db.rawQuery("SELECT id FROM calls WHERE state IN ('OBSERVING','OBSERVING_PROCESSED')", null).use { cursor ->
                val missing = buildList {
                    while (cursor.moveToNext()) if (cursor.getString(0) !in present) add(cursor.getString(0))
                }
                missing.forEach { db.delete("calls", "id=?", arrayOf(it)) }
            }
            for (row in rows) {
                if (row.addedAt < activated || row.modified * 1000 < activated || row.size <= 0) {
                    db.delete("calls", "id=? AND state IN ('OBSERVING','OBSERVING_PROCESSED')", arrayOf(row.uri))
                    continue
                }
                db.rawQuery("SELECT size, modified, stable_since, zone, state, name FROM calls WHERE id=?",
                    arrayOf(row.uri)).use { old ->
                    if (!old.moveToFirst()) {
                        // MediaStore can assign a new URI during reindexing. Preserve terminal state
                        // and all hints for the same metadata fingerprint; never read audio content.
                        val reindexed = db.rawQuery("""SELECT id,state FROM calls WHERE name=? AND size=? AND modified=?
                            AND state IN ('UNSHARED','PROCESSED','REMOVED','DISMISSED') LIMIT 1""",
                            arrayOf(row.name, row.size.toString(), row.modified.toString())).use { match ->
                            if (!match.moveToFirst()) false else {
                                val oldId = match.getString(0)
                                val state = match.getString(1)
                                db.update("calls", ContentValues().apply {
                                    put("id", row.uri)
                                    if (state == "REMOVED") put("state", "UNSHARED")
                                }, "id=?", arrayOf(oldId))
                                // A reindexed unshared call is not a newly discovered call for alerts.
                                if (state == "UNSHARED") {
                                    setting("notified_ids")?.let { ids ->
                                        setting("notified_ids", ids.split('\n')
                                            .map { if (it == oldId) row.uri else it }.sorted().joinToString("\n"))
                                    }
                                }
                                true
                            }
                        }
                        if (reindexed) return@use
                        val hints = CallFilename.parse(row.name, zone)
                        db.insertOrThrow("calls", null, ContentValues().apply {
                            put("id", row.uri); put("name", row.name); put("size", row.size)
                            put("modified", row.modified); put("stable_since", now); put("zone", zone.id)
                            put("state", "OBSERVING"); put("counterpart", hints.counterpart)
                            put("occurred_at", hints.occurredAt)
                        })
                    } else if (old.getString(4) == "REMOVED") {
                        db.update("calls", ContentValues().apply { put("state", "UNSHARED") }, "id=?", arrayOf(row.uri))
                    } else if (old.getString(4).startsWith("OBSERVING")) {
                        if (old.getLong(0) != row.size || old.getLong(1) != row.modified || old.getString(5) != row.name || now < old.getLong(2)) {
                            val hints = CallFilename.parse(row.name, ZoneId.of(old.getString(3)))
                            db.update("calls", ContentValues().apply {
                                put("counterpart", hints.counterpart); put("occurred_at", hints.occurredAt)
                                put("size", row.size); put("modified", row.modified)
                                put("name", row.name); put("stable_since", now)
                            }, "id=?", arrayOf(row.uri))
                        } else if (now - old.getLong(2) >= STABILITY_MS) {
                            val hints = CallFilename.parse(row.name, ZoneId.of(old.getString(3)))
                            db.update("calls", ContentValues().apply {
                                put("state", if (old.getString(4) == "OBSERVING_PROCESSED") "PROCESSED" else "UNSHARED")
                                put("counterpart", hints.counterpart)
                                put("occurred_at", hints.occurredAt)
                            }, "id=?", arrayOf(row.uri))
                        }
                    }
                }
            }
            // Reconcile fingerprints before marking disappearance, so reindexing preserves state.
            db.rawQuery("SELECT id FROM calls WHERE state='UNSHARED'", null).use { cursor ->
                val missing = buildList {
                    while (cursor.moveToNext()) if (cursor.getString(0) !in present) add(cursor.getString(0))
                }
                missing.forEach { id -> db.update("calls", ContentValues().apply { put("state", "REMOVED") },
                    "id=?", arrayOf(id)) }
            }
            db.setTransactionSuccessful()
        } finally { db.endTransaction() }
        return db.rawQuery("SELECT 1 FROM calls WHERE state IN ('OBSERVING','OBSERVING_PROCESSED') LIMIT 1", null).use { it.moveToFirst() }
    }

    fun calls(): List<CallRecord> = readableDatabase.rawQuery(
        "SELECT id,name,counterpart,occurred_at,state FROM calls WHERE state IN ('UNSHARED','PROCESSED','REMOVED','DISMISSED') ORDER BY stable_since DESC,id", null,
    ).use { cursor -> buildList {
        while (cursor.moveToNext()) add(CallRecord(cursor.getString(0), cursor.getString(1),
            cursor.getString(2), cursor.getString(3), CallStatus.valueOf(cursor.getString(4))))
    } }
    override fun pendingCalls(): List<CallRecord> = readableDatabase.rawQuery(
        "SELECT id,name,counterpart,occurred_at FROM calls WHERE state IN ('UNSHARED','OBSERVING') ORDER BY stable_since DESC,id", null,
    ).use { cursor -> buildList {
        while (cursor.moveToNext()) add(CallRecord(cursor.getString(0), cursor.getString(1),
            cursor.getString(2), cursor.getString(3), CallStatus.UNSHARED))
    } }
    override fun markProcessed(id: String): Boolean {
        val db = writableDatabase
        db.beginTransaction()
        return try {
            val changed = db.update("calls", ContentValues().apply { put("state", "PROCESSED") },
                "id=? AND state='UNSHARED'", arrayOf(id)) +
                db.update("calls", ContentValues().apply { put("state", "OBSERVING_PROCESSED") },
                    "id=? AND state='OBSERVING'", arrayOf(id))
            db.setTransactionSuccessful()
            changed == 1
        } finally { db.endTransaction() }
    }

    fun dismiss(id: String): Boolean = writableDatabase.update("calls", ContentValues().apply {
        put("state", "DISMISSED")
    }, "id=? AND state='UNSHARED'", arrayOf(id)) == 1

    companion object { internal const val STABILITY_MS = 30_000L }
}
