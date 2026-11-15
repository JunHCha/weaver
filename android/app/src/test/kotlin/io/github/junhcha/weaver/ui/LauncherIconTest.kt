package io.github.junhcha.weaver.ui

import java.io.File
import javax.xml.parsers.DocumentBuilderFactory
import kotlin.math.hypot
import org.junit.Assert.*
import org.junit.Test
import org.w3c.dom.Element

class LauncherIconTest {
    private val android = "http://schemas.android.com/apk/res/android"
    private fun read(file: File) = DocumentBuilderFactory.newInstance().apply { isNamespaceAware = true }
        .newDocumentBuilder().parse(file)
    private fun resource(name: String) = read(File(System.getProperty("weaver.resDir"), name))

    @Test fun foregroundPreservesSvgPathsAndFitsSafeZone() {
        val source = read(File(System.getProperty("weaver.designDir"), "logo.svg")).getElementsByTagName("path")
        val vector = resource("drawable/ic_launcher_foreground.xml")
        val paths = vector.getElementsByTagName("path")
        assertEquals(source.length, paths.length)
        for (i in 0 until source.length) {
            val svg = source.item(i) as Element
            val path = paths.item(i) as Element
            assertEquals(svg.getAttribute("d"), path.getAttributeNS(android, "pathData"))
            assertEquals(svg.getAttribute("stroke-width"), path.getAttributeNS(android, "strokeWidth"))
            val resColor = path.getAttributeNS(android, "strokeColor").removePrefix("@color/")
            val colors = resource("values/colors.xml").getElementsByTagName("color")
            val hex = (0 until colors.length).map { colors.item(it) as Element }
                .first { it.getAttribute("name") == resColor }.textContent
            assertEquals(svg.getAttribute("stroke"), hex)
            assertEquals(svg.getAttribute("stroke-linecap").ifEmpty { "round" }, path.getAttributeNS(android, "strokeLineCap"))
        }
        val group = vector.getElementsByTagName("group").item(0) as Element
        val scale = group.getAttributeNS(android, "scaleX").toFloat()
        assertEquals(scale, group.getAttributeNS(android, "scaleY").toFloat())
        assertEquals(54f, group.getAttributeNS(android, "translateX").toFloat() + 16 * scale, .001f)
        assertEquals(54f, group.getAttributeNS(android, "translateY").toFloat() + 16 * scale, .001f)
        // SVG stroke-inclusive bounds: x 1.3..30.7, y 2.7..29.3.
        // Even every corner of this enclosing box fits the adaptive icon's 66dp circle.
        assertTrue(hypot(14.7f, 13.3f) * scale <= 33f)
    }

    @Test fun monochromeUsesMarkStrokesWithoutOpaqueBackground() {
        val paths = resource("drawable/ic_launcher_monochrome.xml").getElementsByTagName("path")
        assertEquals(3, paths.length)
        for (i in 0 until paths.length) {
            assertEquals("@color/weaver_ink", (paths.item(i) as Element).getAttributeNS(android, "strokeColor"))
        }
        val mono = resource("mipmap-anydpi/ic_launcher.xml").getElementsByTagName("monochrome").item(0) as Element
        assertEquals("@drawable/ic_launcher_monochrome", mono.getAttributeNS(android, "drawable"))
    }
}
