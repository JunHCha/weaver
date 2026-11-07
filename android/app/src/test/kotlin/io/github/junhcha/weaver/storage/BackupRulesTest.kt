package io.github.junhcha.weaver.storage

import java.io.File
import javax.xml.parsers.DocumentBuilderFactory
import org.junit.Assert.assertEquals
import org.junit.Test
import org.w3c.dom.Element

/**
 * Originals and exchange files live in files/ and cache/; these rules must exclude every
 * domain, for Android 12+ cloud backup and device-to-device transfer and for the
 * Android 8-11 full-backup format. The merged manifest attributes are checked in
 * ManifestBackupTest.
 */
class BackupRulesTest {
    private val res = File(requireNotNull(System.getProperty("weaver.resDir")))

    @Test
    fun dataExtractionRulesExcludeAllDomainsForCloudAndDeviceTransfer() {
        val root = parse("xml/data_extraction_rules.xml")
        listOf("cloud-backup", "device-transfer").forEach { section ->
            val nodes = root.getElementsByTagName(section)
            assertEquals("one <$section>", 1, nodes.length)
            assertExcludesEverything(section, nodes.item(0) as Element)
        }
    }

    @Test
    fun legacyFullBackupRulesExcludeAllDomains() {
        assertExcludesEverything("full-backup-content", parse("xml/backup_rules.xml"))
    }

    private fun assertExcludesEverything(where: String, section: Element) {
        val rules = section.childElements()
        assertEquals("$where must not include anything", emptyList<String>(), rules.filter { it.tagName == "include" }.map { it.domain })
        val excluded = rules.filter { it.tagName == "exclude" && it.getAttribute("path") == "." }.map { it.domain }.toSet()
        assertEquals("$where excluded domains", ALL_DOMAINS, excluded)
    }

    private fun parse(path: String): Element =
        DocumentBuilderFactory.newInstance().newDocumentBuilder().parse(File(res, path)).documentElement

    private fun Element.childElements(): List<Element> =
        (0 until childNodes.length).map { childNodes.item(it) }.filterIsInstance<Element>()

    private val Element.domain: String get() = getAttribute("domain")

    private companion object {
        val ALL_DOMAINS = setOf(
            "root", "file", "database", "sharedpref", "external",
            "device_root", "device_file", "device_database", "device_sharedpref",
        )
    }
}
