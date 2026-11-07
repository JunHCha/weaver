package io.github.junhcha.weaver.ui

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import androidx.annotation.StringRes
import androidx.core.content.ContextCompat
import io.github.junhcha.weaver.R

/** Features that need a runtime permission. Each one asks only when the user opts in. */
enum class Feature(@param:StringRes val title: Int, @param:StringRes val description: Int) {
    NOTIFICATIONS(R.string.feature_notifications, R.string.feature_notifications_desc),
    SHEPHERD(R.string.feature_shepherd, R.string.feature_shepherd_desc),
    CALENDAR(R.string.feature_calendar, R.string.feature_calendar_desc);

    /** Runtime permissions this feature needs on [sdk]; empty when none is required. */
    @SuppressLint("InlinedApi") // API 33 constants are only returned when sdk >= 33.
    fun permissions(sdk: Int = Build.VERSION.SDK_INT): List<String> = when (this) {
        NOTIFICATIONS ->
            if (sdk >= Build.VERSION_CODES.TIRAMISU) listOf(Manifest.permission.POST_NOTIFICATIONS) else emptyList()
        SHEPHERD ->
            if (sdk >= Build.VERSION_CODES.TIRAMISU) {
                listOf(Manifest.permission.READ_MEDIA_AUDIO)
            } else {
                listOf(Manifest.permission.READ_EXTERNAL_STORAGE)
            }
        CALENDAR -> listOf(Manifest.permission.READ_CALENDAR, Manifest.permission.WRITE_CALENDAR)
    }

    fun isGranted(context: Context): Boolean = permissions().all {
        ContextCompat.checkSelfPermission(context, it) == PackageManager.PERMISSION_GRANTED
    }
}
