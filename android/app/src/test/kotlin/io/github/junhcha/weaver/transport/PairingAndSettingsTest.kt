package io.github.junhcha.weaver.transport

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
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

/** A box whose key is gone (restored phone, reset lock screen): sealing worked once, opening never does. */
internal class LostKeyBox : SecretBox {
    override fun seal(plain: ByteArray) = plain
    override fun open(sealed: ByteArray): ByteArray = throw java.security.GeneralSecurityException("key_gone")
}

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class PairingAndSettingsTest {
    private val pki = TestPki.default
    private val token = "T".repeat(43)
    private lateinit var settings: TransportSettings
    private lateinit var context: Context

    private fun code(url: String = "https://100.106.199.83:8787", sha: String = pki.caSha256, der: ByteArray = pki.caDer,
                     t: String = token, v: Int = 1, serverId: String = "srv-1") =
        """{"v":$v,"server_url":"$url","server_id":"$serverId","device_id":"dev-1","token":"$t","ca_sha256":"$sha","ca_der_b64":"${
            java.util.Base64.getEncoder().encodeToString(der)}"}"""

    @Before fun setup() {
        context = ApplicationProvider.getApplicationContext()
        val prefs = context.getSharedPreferences("transport-test", Context.MODE_PRIVATE)
        prefs.edit().clear().commit()
        settings = TransportSettings(prefs, ReverseBox())
    }

    private fun sha(bytes: ByteArray) = java.security.MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

    @Test fun parsesTheServersQrPayload() {
        val pairing = PairingCodec.parse(code())
        assertEquals("https://100.106.199.83:8787", pairing.baseUrl)
        assertEquals("srv-1", pairing.serverId); assertEquals("dev-1", pairing.deviceId)
        assertEquals(token, pairing.token)
        assertEquals(pki.caSha256, pairing.caSha256)
        assertArrayEquals(pki.caDer, pairing.caDer)
    }

    @Test fun rejectsPlainHttpWrongVersionBadCaAndOddAddresses() {
        val leafDer = pki.leaf("ip:127.0.0.1").getCertificate("server").encoded
        listOf(
            code(url = "http://100.106.199.83:8787"), code(v = 2), code(sha = "zz"), code(sha = "ab".repeat(32)),
            code(t = "short"), code(url = "https://user:pw@100.106.199.83:8787"),
            code(url = "https://100.106.199.83:8787/path?x=1"), "not json", "{}",
            code(der = byteArrayOf(1, 2, 3), sha = sha(byteArrayOf(1, 2, 3))),
            code(der = leafDer, sha = sha(leafDer)), // a leaf is not a CA: pairing must carry the CA
        ).forEach { text -> assertThrows(text, PairingException::class.java) { PairingCodec.parse(text) } }
    }

    @Test fun tokenIsStoredSealedAndRoundTrips() {
        settings.pair(PairingCodec.parse(code()))
        assertTrue(settings.paired)
        val raw = context.getSharedPreferences("transport-test", Context.MODE_PRIVATE).all.values.joinToString("|")
        assertFalse("token must not be stored in plain text", raw.contains(token))
        val back = settings.pairing()!!
        assertEquals(token, back.token); assertArrayEquals(pki.caDer, back.caDer)
    }

    @Test fun aLostKeystoreKeyMeansPairAgainNotConnected() {
        val prefs = context.getSharedPreferences("transport-test", Context.MODE_PRIVATE)
        TransportSettings(prefs, ReverseBox()).pair(PairingCodec.parse(code()))
        val broken = TransportSettings(prefs, LostKeyBox())
        assertFalse(broken.needsRepair)
        assertNull(broken.pairing())
        assertTrue("an unreadable token is never shown as connected", broken.needsRepair)
    }

    @Test fun featuresStartOnSyncthingAndFollowTheServersCapabilities() {
        TransportFeature.entries.forEach { assertEquals(TransportRoute.FILES, settings.route(it)) }
        settings.pair(PairingCodec.parse(code()))
        val caps = Json.parseToJsonElement("""{"transports":{"capture":"http","review":"files","action":"http"},"features":{"alerts":true}}""").jsonObject
        val changed = settings.applyCapabilities(caps)
        assertEquals(listOf(TransportFeature.CAPTURE, TransportFeature.ACTION_RESULT), changed)
        assertEquals(TransportRoute.HTTP, settings.route(TransportFeature.CAPTURE))
        assertEquals(TransportRoute.FILES, settings.route(TransportFeature.VERDICT))
        assertTrue(settings.feature(ServerFeature.ALERTS)); assertFalse(settings.feature(ServerFeature.PIPELINE))
        assertTrue(settings.anyHttp)
        // The server switches capture back: the change is reported so unsent items can move.
        assertEquals(listOf(TransportFeature.CAPTURE), settings.applyCapabilities(
            Json.parseToJsonElement("""{"transports":{"capture":"files","action":"http"}}""").jsonObject))
        assertFalse("a missing features block means the server offers nothing optional", settings.feature(ServerFeature.ALERTS))
    }

    @Test fun unpairingForgetsTheServerAndFallsBackToSyncthing() {
        settings.pair(PairingCodec.parse(code()))
        settings.applyCapabilities(Json.parseToJsonElement("""{"transports":{"review":"http"}}""").jsonObject)
        settings.unpair()
        assertFalse(settings.paired)
        assertNull(settings.pairing())
        assertEquals(TransportRoute.FILES, settings.route(TransportFeature.VERDICT))
    }

    @Test fun pairingAnotherServerResetsRoutesToSyncthingUntilItAnnouncesItsOwn() {
        settings.pair(PairingCodec.parse(code()))
        settings.applyCapabilities(Json.parseToJsonElement("""{"transports":{"review":"http"}}""").jsonObject)
        settings.pair(PairingCodec.parse(code(serverId = "srv-2")))
        assertEquals(TransportRoute.FILES, settings.route(TransportFeature.VERDICT))
    }

    @Test fun featureNamesMatchTheServerCapabilitiesKeys() {
        assertEquals(listOf("capture", "review", "action"), TransportFeature.entries.map { it.wire })
    }

    @Test fun manifestKeepsTokensOutOfBackupsAndRequestsInternet() {
        val manifest = java.io.File(System.getProperty("weaver.resDir")!!, "../AndroidManifest.xml").readText()
        assertTrue(manifest.contains("android.permission.INTERNET"))
        assertTrue(manifest.contains("android:allowBackup=\"false\""))
        val rules = java.io.File(System.getProperty("weaver.resDir")!!, "xml/data_extraction_rules.xml").readText()
        assertTrue(rules.contains("domain=\"sharedpref\"") && rules.contains("domain=\"file\""))
    }
}
