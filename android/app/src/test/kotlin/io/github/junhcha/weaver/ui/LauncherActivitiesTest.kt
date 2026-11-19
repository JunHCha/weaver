package io.github.junhcha.weaver.ui

import java.io.File
import javax.xml.parsers.DocumentBuilderFactory
import org.junit.Assert.assertEquals
import org.junit.Test
import org.w3c.dom.Element

/** A debug install must show one launcher icon: the call-briefing experiment opens from the settings screen instead. */
class LauncherActivitiesTest {
    private val main = File(System.getProperty("weaver.resDir")!!).parentFile
    private fun launchers(manifest: File): List<String> {
        val doc = DocumentBuilderFactory.newInstance().apply { isNamespaceAware = false }.newDocumentBuilder().parse(manifest)
        val activities = doc.getElementsByTagName("activity")
        return (0 until activities.length).map { activities.item(it) as Element }.filter { activity ->
            val categories = activity.getElementsByTagName("category")
            (0 until categories.length).any { (categories.item(it) as Element).getAttribute("android:name") == "android.intent.category.LAUNCHER" }
        }.map { it.getAttribute("android:name") }
    }

    @Test fun mainDeclaresTheOnlyLauncherAndDebugAddsNone() {
        val mainLaunchers = launchers(File(main, "AndroidManifest.xml"))
        val debugLaunchers = launchers(File(main.parentFile, "debug/AndroidManifest.xml"))
        assertEquals(listOf(".MainActivity"), mainLaunchers)
        assertEquals("the debug manifest must not add a second launcher icon", emptyList<String>(), debugLaunchers)
        assertEquals(1, mainLaunchers.size + debugLaunchers.size)
    }

    @Test fun theCallBriefSetupActivityStaysExportedForAdb() {
        val text = File(main.parentFile, "debug/AndroidManifest.xml").readText()
        assertEquals(true, Regex("CallBriefSetupActivity\"[^>]*android:exported=\"true\"").containsMatchIn(text))
    }
}
