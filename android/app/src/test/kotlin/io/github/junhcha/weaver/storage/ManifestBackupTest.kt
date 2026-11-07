package io.github.junhcha.weaver.storage

import android.app.Application
import android.content.pm.ApplicationInfo
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.io.File
import java.util.Properties
import javax.xml.parsers.DocumentBuilderFactory
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config
import org.w3c.dom.Element

/** The merged manifest, as installed, disables backup and points at the exclusion rules. */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [36])
class ManifestBackupTest {
    @Test
    fun mergedManifestDisablesBackupAndWiresRules() {
        val application = mergedManifest().getElementsByTagName("application").item(0) as Element
        assertEquals("false", application.getAttributeNS(ANDROID_NS, "allowBackup"))
        assertEquals("@xml/data_extraction_rules", application.getAttributeNS(ANDROID_NS, "dataExtractionRules"))
        assertEquals("@xml/backup_rules", application.getAttributeNS(ANDROID_NS, "fullBackupContent"))

        val info = ApplicationProvider.getApplicationContext<Application>().applicationInfo
        assertEquals("FLAG_ALLOW_BACKUP", 0, info.flags and ApplicationInfo.FLAG_ALLOW_BACKUP)
    }

    @Test
    fun sensitiveDirectoriesLiveUnderExcludedDomains() {
        val context = ApplicationProvider.getApplicationContext<Application>()
        // files/ is the "file" domain and cache/ is under "root"; both are excluded entirely.
        assertTrue(AppStorage.originalsDir(context).path.startsWith(context.filesDir.path))
        assertTrue(AppStorage.exchangeDir(context).path.startsWith(context.filesDir.path))
        assertTrue(AppStorage.exchangeCacheDir(context).path.startsWith(context.cacheDir.path))
    }

    /** AGP records the merged manifest used for unit tests in test_config.properties. */
    private fun mergedManifest(): Element {
        val config = Properties().apply {
            ClassLoader.getSystemResourceAsStream("com/android/tools/test_config.properties")!!.use(::load)
        }
        val factory = DocumentBuilderFactory.newInstance().apply { isNamespaceAware = true }
        return factory.newDocumentBuilder().parse(File(config.getProperty("android_merged_manifest"))).documentElement
    }

    private companion object {
        const val ANDROID_NS = "http://schemas.android.com/apk/res/android"
    }
}
