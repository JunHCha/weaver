package io.github.junhcha.weaver.transport

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** Reversible stand-in for the Keystore box: proves the settings never keep the token in the clear. */
internal class ReverseBox : SecretBox {
    override fun seal(plain: ByteArray) = plain.reversedArray()
    override fun open(sealed: ByteArray) = sealed.reversedArray()
}

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class PairingAndSettingsTest {
    private val fp = "ab".repeat(32)
    private val token = "T".repeat(43)
    private fun code(url: String = "https://100.106.199.83:8787", f: String = fp, t: String = token, v: Int = 1) =
        """{"v":$v,"url":"$url","fp":"$f","token":"$t"}"""
    private lateinit var settings: TransportSettings
    private lateinit var context: Context

    @Before fun setup() {
        context = ApplicationProvider.getApplicationContext()
        val prefs = context.getSharedPreferences("transport-test", Context.MODE_PRIVATE)
        prefs.edit().clear().commit()
        settings = TransportSettings(prefs, ReverseBox())
    }

    @Test fun parsesTheQrPayload() {
        val pairing = PairingCodec.parse(code())
        assertEquals("https://100.106.199.83:8787", pairing.baseUrl)
        assertEquals(fp, pairing.fingerprint)
        assertEquals(token, pairing.token)
    }

    @Test fun rejectsPlainHttpWrongVersionBadFingerprintAndOddAddresses() {
        listOf(
            code(url = "http://100.106.199.83:8787"), code(v = 2), code(f = "zz"), code(f = "ab".repeat(31)),
            code(t = "short"), code(url = "https://user:pw@100.106.199.83:8787"),
            code(url = "https://100.106.199.83:8787/path?x=1"), "not json", "{}",
        ).forEach { text -> assertThrows(text, PairingException::class.java) { PairingCodec.parse(text) } }
    }

    @Test fun tokenIsStoredSealedAndRoundTrips() {
        settings.pair(PairingCodec.parse(code()))
        assertTrue(settings.paired)
        val raw = context.getSharedPreferences("transport-test", Context.MODE_PRIVATE).all.values.joinToString("|")
        assertFalse("token must not be stored in plain text", raw.contains(token))
        assertEquals(token, settings.pairing()!!.token)
    }

    @Test fun featuresDefaultToSyncthingAndHttpNeedsAPairing() {
        TransportFeature.entries.forEach { assertEquals(TransportRoute.FILES, settings.route(it)) }
        assertThrows(IllegalArgumentException::class.java) { settings.setRoute(TransportFeature.CAPTURE, TransportRoute.HTTP) }
        settings.pair(PairingCodec.parse(code()))
        settings.setRoute(TransportFeature.CAPTURE, TransportRoute.HTTP)
        assertEquals(TransportRoute.HTTP, settings.route(TransportFeature.CAPTURE))
        assertEquals(TransportRoute.FILES, settings.route(TransportFeature.VERDICT))
        assertTrue(settings.anyHttp)
    }

    @Test fun unpairingForgetsTheServerAndFallsBackToSyncthing() {
        settings.pair(PairingCodec.parse(code()))
        settings.setRoute(TransportFeature.VERDICT, TransportRoute.HTTP)
        settings.unpair()
        assertFalse(settings.paired)
        assertNull(settings.pairing())
        assertEquals(TransportRoute.FILES, settings.route(TransportFeature.VERDICT))
    }

    @Test fun featureNamesMatchTheServerSettings() {
        assertEquals(listOf("capture", "verdict", "action-result"), TransportFeature.entries.map { it.wire })
    }

    @Test fun manifestKeepsTokensOutOfBackupsAndRequestsInternet() {
        val manifest = java.io.File(System.getProperty("weaver.resDir")!!, "../AndroidManifest.xml").readText()
        assertTrue(manifest.contains("android.permission.INTERNET"))
        assertTrue(manifest.contains("android:allowBackup=\"false\""))
        // The transport state lives under files/ and shared_prefs/, both excluded by the existing rules.
        val rules = java.io.File(System.getProperty("weaver.resDir")!!, "xml/data_extraction_rules.xml").readText()
        assertTrue(rules.contains("domain=\"sharedpref\"") && rules.contains("domain=\"file\""))
    }
}
