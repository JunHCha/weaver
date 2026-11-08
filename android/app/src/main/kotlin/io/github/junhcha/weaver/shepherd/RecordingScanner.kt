package io.github.junhcha.weaver.shepherd

import android.content.ContentUris
import android.content.Context
import android.os.Build
import android.provider.MediaStore

internal class RecordingScanner(private val context: Context) {
    @Suppress("DEPRECATION")
    fun scan(activatedAt: Long): List<RecordingMetadata> {
        val path = if (Build.VERSION.SDK_INT >= 29) MediaStore.Audio.Media.RELATIVE_PATH else MediaStore.Audio.Media.DATA
        val columns = arrayOf(MediaStore.Audio.Media._ID, MediaStore.Audio.Media.DISPLAY_NAME,
            MediaStore.Audio.Media.SIZE, MediaStore.Audio.Media.DATE_MODIFIED, MediaStore.Audio.Media.DATE_ADDED, path)
        val collection = MediaStore.Audio.Media.EXTERNAL_CONTENT_URI
        val selection = "${MediaStore.Audio.Media.DATE_ADDED} >= ? AND " +
            if (Build.VERSION.SDK_INT >= 29) "$path = ?" else "$path LIKE ?"
        val pathArg = if (Build.VERSION.SDK_INT >= 29) "Recordings/Call/" else "%/Recordings/Call/%"
        return context.contentResolver.query(collection, columns, selection,
            arrayOf((activatedAt / 1000).toString(), pathArg), null)?.use { cursor -> buildList {
            while (cursor.moveToNext()) {
                val location = cursor.getString(5)
                if (!RecordingPaths.matches(if (Build.VERSION.SDK_INT >= 29) location else null,
                        if (Build.VERSION.SDK_INT < 29) location else null)) continue
                add(RecordingMetadata(ContentUris.withAppendedId(collection, cursor.getLong(0)).toString(),
                    cursor.getString(1) ?: "", cursor.getLong(2), cursor.getLong(3), cursor.getLong(4) * 1000))
            }
        } } ?: error("MediaStore query unavailable")
    }
}
