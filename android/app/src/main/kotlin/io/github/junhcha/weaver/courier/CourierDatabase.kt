package io.github.junhcha.weaver.courier

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import io.github.junhcha.weaver.schema.Manifest
import io.github.junhcha.weaver.schema.WeaverCodec
import java.io.File

/** Durable ID assignment precedes any external publication. Never stores source content in logs. */
class CourierDatabase(context: Context) : SQLiteOpenHelper(
    context, File(context.filesDir, "courier.sqlite3").absolutePath, null, 1,
), java.io.Closeable {
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("""CREATE TABLE captures (
            capture_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE,
            original_uri TEXT NOT NULL UNIQUE, manifest TEXT NOT NULL,
            staged_path TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
            changed_at INTEGER NOT NULL DEFAULT 0)""")
        db.execSQL("""CREATE TABLE diagnostics (
            folder TEXT NOT NULL, name TEXT NOT NULL, code TEXT NOT NULL,
            found_at INTEGER NOT NULL, UNIQUE(folder,name,code))""")
    }
    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit
    data class Capture(val manifest: Manifest, val stagedPath: String, val state: String, val changedAt: Long)

    fun prepare(uri: String, manifest: Manifest, staged: File): Capture {
        val db = writableDatabase
        db.beginTransaction()
        try {
            val values = ContentValues().apply {
                put("capture_id", manifest.captureId); put("sha256", manifest.sha256)
                put("original_uri", uri); put("manifest", WeaverCodec.encode(manifest))
                put("staged_path", staged.absolutePath)
            }
            db.insertWithOnConflict("captures", null, values, SQLiteDatabase.CONFLICT_IGNORE)
            val result = captures().firstOrNull { it.manifest.sha256 == manifest.sha256 }
                ?: error("original_uri_changed")
            db.setTransactionSuccessful()
            return result
        } finally { db.endTransaction() }
    }

    fun captures(): List<Capture> = readableDatabase.rawQuery(
        "SELECT manifest, staged_path, state, changed_at FROM captures", null,
    ).use { c -> buildList {
        while (c.moveToNext()) add(Capture(
            WeaverCodec.json.decodeFromString<Manifest>(c.getString(0)), c.getString(1), c.getString(2), c.getLong(3),
        ))
    } }

    fun state(id: String, state: String, now: Long) {
        writableDatabase.update("captures", ContentValues().apply {
            put("state", state); put("changed_at", now)
        }, "capture_id=?", arrayOf(id))
    }

    fun diagnostic(folder: String, name: String, code: String) {
        writableDatabase.insertWithOnConflict("diagnostics", null, ContentValues().apply {
            put("folder", folder); put("name", name); put("code", code)
            put("found_at", System.currentTimeMillis())
        }, SQLiteDatabase.CONFLICT_IGNORE)
    }
    fun diagnostics(): List<String> = readableDatabase.rawQuery(
        "SELECT folder,name,code,found_at FROM diagnostics ORDER BY found_at DESC LIMIT 200", null,
    ).use { c -> buildList {
        while (c.moveToNext()) add("${c.getString(0)}/${c.getString(1)}: ${c.getString(2)} @ ${java.time.Instant.ofEpochMilli(c.getLong(3))}")
    } }
}
