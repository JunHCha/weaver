package io.github.junhcha.weaver.storage

import android.content.Context
import java.io.File

/**
 * App-private locations for sensitive data. All of them sit under backup domains that
 * res/xml/data_extraction_rules.xml and res/xml/backup_rules.xml exclude entirely, so
 * originals and exchange files never reach cloud backup or device-to-device transfer.
 */
object AppStorage {
    /** Originals (audio, shared text) staged before they are written to the sync outbox. */
    const val ORIGINALS_DIR = "originals"

    /** Courier protocol files (manifests, receipts, review packages, verdicts) kept by the app. */
    const val EXCHANGE_DIR = "exchange"

    fun originalsDir(context: Context): File = File(context.filesDir, ORIGINALS_DIR)

    fun exchangeDir(context: Context): File = File(context.filesDir, EXCHANGE_DIR)

    fun exchangeCacheDir(context: Context): File = File(context.cacheDir, EXCHANGE_DIR)
}
