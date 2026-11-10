package io.github.junhcha.weaver.shepherd.autoshare

import android.accessibilityservice.AccessibilityServiceInfo
import android.content.Context
import androidx.test.core.app.ApplicationProvider
import io.github.junhcha.weaver.R
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** The accessibility service's XML as built for this build type. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class AutoShareServiceConfigTest {
    private val ns = "http://schemas.android.com/apk/res/android"

    private fun parse(block: (android.content.res.XmlResourceParser) -> Unit) {
        val context = ApplicationProvider.getApplicationContext<Context>()
        context.resources.getXml(R.xml.auto_share_service).use { parser ->
            while (parser.next() != org.xmlpull.v1.XmlPullParser.START_TAG) continue
            block(parser)
        }
    }

    @Test fun includesNotImportantViewsSoContainerNodesAreVisible() = parse { parser ->
        val flags = parser.getAttributeIntValue(ns, "accessibilityFlags", 0)
        assertTrue(flags and AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS != 0)
        assertTrue(flags and AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS != 0)
    }

    @Test fun observedPackagesIncludeThisBuildsOwnPackage() = parse { parser ->
        val context = ApplicationProvider.getApplicationContext<Context>()
        val ref = parser.getAttributeResourceValue(ns, "packageNames", 0)
        val raw = if (ref != 0) context.getString(ref) else parser.getAttributeValue(ns, "packageNames")
        assertEquals(Selectors.allowedPackages(context.packageName), raw.split(",").toSet())
    }
}
