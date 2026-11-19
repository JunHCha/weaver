package io.github.junhcha.weaver.phone

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.database.sqlite.SQLiteException
import kotlinx.serialization.json.*
import java.time.Instant

/** Private device state, deliberately outside Fabric and the replaceable HTTP feed cache. */
open class NowStore(context: Context, name: String = "now-state.db") : SQLiteOpenHelper(context, name, null, 1) {
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE snapshot(server TEXT PRIMARY KEY, epoch TEXT NOT NULL, body TEXT NOT NULL)")
        db.execSQL("CREATE TABLE bookmark(server TEXT, epoch TEXT, seq INTEGER NOT NULL, PRIMARY KEY(server,epoch))")
        db.execSQL("CREATE TABLE view_state(server TEXT, epoch TEXT, thread TEXT, revision INTEGER NOT NULL, until_at TEXT, hidden INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(server,epoch,thread))")
        db.execSQL("CREATE TABLE pending(server TEXT, epoch TEXT, client TEXT PRIMARY KEY, path TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL, result TEXT, code TEXT)")
    }
    // Never reset this DB: pending keys/bodies must survive upgrades and downgrades.
    // Add explicit, journal-preserving migrations before incrementing the schema version.
    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        throw SQLiteException("now_state_migration_required_${oldVersion}_$newVersion")
    }
    override fun onDowngrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        throw SQLiteException("now_state_downgrade_unsupported_${oldVersion}_$newVersion")
    }

    fun snapshot(server: String): JsonObject? = readableDatabase.rawQuery("SELECT body FROM snapshot WHERE server=?", arrayOf(server)).use {
        if (it.moveToFirst()) Json.parseToJsonElement(it.getString(0)) as JsonObject else null
    }
    fun saveSnapshot(server: String, epoch: String, body: JsonObject) {
        writableDatabase.insertWithOnConflict("snapshot", null, ContentValues().apply {
            put("server", server); put("epoch", epoch); put("body", body.toString())
        }, SQLiteDatabase.CONFLICT_REPLACE)
    }
    fun ack(server: String, epoch: String): Long = readableDatabase.rawQuery("SELECT seq FROM bookmark WHERE server=? AND epoch=?", arrayOf(server, epoch)).use {
        if (it.moveToFirst()) it.getLong(0) else 0
    }
    fun acknowledge(server: String, epoch: String, head: Long) {
        writableDatabase.insertWithOnConflict("bookmark", null, ContentValues().apply {
            put("server", server); put("epoch", epoch); put("seq", maxOf(ack(server, epoch), head))
        }, SQLiteDatabase.CONFLICT_REPLACE)
    }
    fun resetAck(server: String, epoch: String) {
        writableDatabase.delete("bookmark", "server=? AND epoch=?", arrayOf(server, epoch))
    }
    fun setView(server: String, epoch: String, thread: PhoneThread, until: Instant? = null, hidden: Boolean = false) {
        writableDatabase.insertWithOnConflict("view_state", null, ContentValues().apply {
            put("server", server); put("epoch", epoch); put("thread", thread.id); put("revision", thread.revision)
            put("until_at", until?.toString()); put("hidden", if (hidden) 1 else 0)
            put("payload", viewThread(thread).toString())
        }, SQLiteDatabase.CONFLICT_REPLACE)
    }
    /** Keep a renderable copy so hiding an acknowledged recent fact never makes restoration impossible. */
    private fun viewThread(thread: PhoneThread) = buildJsonObject {
        put("thread_id", thread.id); put("seq", thread.seq); put("revision", thread.revision)
        put("kind", thread.kind.label); put("content", thread.content); put("source", thread.source)
        put("occurred_at", thread.occurredAt.toString()); put("recorded_at", thread.recordedAt.toString())
        put("counterpart", thread.counterpart); put("tags", JsonArray(thread.tags.map(::JsonPrimitive)))
        put("direction", thread.direction?.label); put("field", thread.field)
        put("effective_deadline", thread.deadline?.toString())
        put("effective_schedule", thread.scheduleAt?.let { buildJsonObject { put("start", it.toString()) } } ?: JsonNull)
        put("presentation", thread.presentation?.name?.lowercase()); put("open", thread.open); put("retracted", thread.retracted)
        put("resolves", thread.resolves); put("retracts", thread.retracts); put("updates", thread.updates)
        put("executes", thread.executes); put("amends", thread.amends)
        thread.mentions?.let { put("mentions", JsonArray(it.map(::JsonPrimitive))) }
        thread.correction?.let { fix -> put("correction", buildJsonObject {
            put("target", fix.target); put("add", JsonArray(fix.add.map(::JsonPrimitive))); put("remove", JsonArray(fix.remove.map(::JsonPrimitive)))
        }) }
    }
    fun savedViews(server: String, epoch: String): List<PhoneThread> {
        val damaged = mutableListOf<String>()
        val rows = readableDatabase.rawQuery(
            "SELECT thread,payload,until_at FROM view_state WHERE server=? AND epoch=?", arrayOf(server, epoch),
        ).use { cursor -> buildList {
            while (cursor.moveToNext()) {
                val id = cursor.getString(0)
                val thread = runCatching {
                    if (!cursor.isNull(2)) Instant.parse(cursor.getString(2))
                    HttpPhoneReadModel.thread(Json.parseToJsonElement(cursor.getString(1)) as JsonObject).also { require(it.id == id) }
                }.getOrNull()
                if (thread == null) damaged += id else add(thread)
            }
        } }
        damaged.forEach { restore(server, epoch, it) }
        return rows
    }
    fun restore(server: String, epoch: String, thread: String) {
        writableDatabase.delete("view_state", "server=? AND epoch=? AND thread=?", arrayOf(server, epoch, thread))
    }
    fun suppressed(server: String, epoch: String, thread: PhoneThread, now: Instant): Boolean {
        val active = readableDatabase.rawQuery("SELECT revision,until_at,hidden FROM view_state WHERE server=? AND epoch=? AND thread=?", arrayOf(server, epoch, thread.id)).use {
            if (!it.moveToFirst()) return false
            it.getLong(0) == thread.revision && thread.retracted != true &&
                (it.getInt(2) == 1 || (!it.isNull(1) && Instant.parse(it.getString(1)).isAfter(now)))
        }
        if (!active) restore(server, epoch, thread.id)
        return active
    }
    fun nextExpiry(server: String, epoch: String, now: Instant): Instant? = readableDatabase.rawQuery(
        "SELECT until_at FROM view_state WHERE server=? AND epoch=? AND until_at IS NOT NULL AND hidden=0", arrayOf(server, epoch),
    ).use { buildList { while (it.moveToNext()) add(Instant.parse(it.getString(0))) }.filter { it.isAfter(now) }.minOrNull() }
    fun enqueue(server: String, epoch: String, client: String, path: String, body: JsonObject) {
        // INSERT OR ABORT: an existing id can never acquire a different body.
        writableDatabase.insertOrThrow("pending", null, ContentValues().apply {
            put("server", server); put("epoch", epoch); put("client", client); put("path", path)
            put("body", body.toString()); put("state", "pending")
        })
    }
    fun operations(server: String): List<PendingRequest> = readableDatabase.rawQuery("SELECT epoch,client,path,body,state,result,code FROM pending WHERE server=? ORDER BY rowid", arrayOf(server)).use {
        buildList { while (it.moveToNext()) add(PendingRequest(it.getString(0), it.getString(1), it.getString(2),
            Json.parseToJsonElement(it.getString(3)) as JsonObject, it.getString(4),
            if (it.isNull(5)) null else Json.parseToJsonElement(it.getString(5)) as JsonObject, it.getString(6))) }
    }
    fun finish(client: String, state: String, result: JsonObject? = null, code: String? = null) {
        writableDatabase.update("pending", ContentValues().apply {
            put("state", state); put("result", result?.toString()); put("code", code)
        }, "client=?", arrayOf(client))
    }
    fun quarantine(server: String, epoch: String) {
        writableDatabase.execSQL("UPDATE pending SET state='quarantined',code='epoch_mismatch' WHERE server=? AND epoch<>? AND state='pending'", arrayOf(server, epoch))
    }
}

data class PendingRequest(val epoch: String, val clientId: String, val path: String, val body: JsonObject,
                          val state: String, val result: JsonObject?, val code: String?)
