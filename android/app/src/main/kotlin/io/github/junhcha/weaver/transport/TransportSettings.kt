package io.github.junhcha.weaver.transport

import android.content.Context
import android.content.SharedPreferences
import android.util.Base64
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/**
 * Courier features whose write path can be switched between Syncthing files and the Tailscale HTTP API.
 * [wire] is the feature name the server settings use (ADR-040 "병행과 복귀의 공통 규칙").
 */
enum class TransportFeature(val wire: String, val label: String) {
    CAPTURE("capture", "수집"),
    VERDICT("verdict", "검토 판정"),
    ACTION_RESULT("action-result", "실행 결과"),
}

enum class TransportRoute(val label: String) { FILES("Syncthing"), HTTP("HTTP") }

/** What the QR code carries: the server address, the pinned certificate fingerprint and the app token. */
data class Pairing(val baseUrl: String, val fingerprint: String, val token: String)

@Serializable
private data class PairingWire(val v: Int, val url: String, val fp: String, val token: String)

/** Thrown for a QR/pasted pairing text that is not an acceptable v1 payload. Messages are shown to the user. */
class PairingException(message: String) : IllegalArgumentException(message)

object PairingCodec {
    private val json = Json { ignoreUnknownKeys = false }
    private val fingerprint = Regex("[0-9a-f]{64}")
    private val tokenShape = Regex("[A-Za-z0-9_-]{32,128}")

    /** `{"v":1,"url":"https://host:port","fp":"<sha256 hex of the server certificate>","token":"<bearer>"}` */
    fun parse(text: String): Pairing {
        val wire = try { json.decodeFromString<PairingWire>(text.trim()) } catch (_: Exception) {
            throw PairingException("페어링 코드를 읽을 수 없습니다")
        }
        if (wire.v != 1) throw PairingException("지원하지 않는 페어링 버전입니다")
        val uri = try { java.net.URI(wire.url) } catch (_: Exception) { throw PairingException("서버 주소가 올바르지 않습니다") }
        if (uri.scheme != "https") throw PairingException("HTTPS 주소만 사용할 수 있습니다")
        if (uri.host.isNullOrEmpty() || uri.userInfo != null || uri.query != null || uri.fragment != null ||
            !(uri.path.isNullOrEmpty() || uri.path == "/")) throw PairingException("서버 주소가 올바르지 않습니다")
        val fp = wire.fp.lowercase()
        if (!fingerprint.matches(fp)) throw PairingException("인증서 지문이 올바르지 않습니다")
        if (!tokenShape.matches(wire.token)) throw PairingException("토큰 형식이 올바르지 않습니다")
        val port = if (uri.port > 0) ":${uri.port}" else ""
        return Pairing("https://${uri.host}$port", fp, wire.token)
    }
}

/** Seals the token with a device-bound key (Android Keystore in production, a fake in tests). */
interface SecretBox {
    fun seal(plain: ByteArray): ByteArray
    fun open(sealed: ByteArray): ByteArray
}

/**
 * Pairing, per-feature route and connection status. Lives in app-private preferences that backup rules exclude;
 * the token is only ever stored sealed. Features default to Syncthing files; HTTP needs a pairing.
 */
class TransportSettings(private val prefs: SharedPreferences, private val box: SecretBox) {
    val paired: Boolean get() = prefs.contains(TOKEN)
    val baseUrl: String? get() = prefs.getString(URL, null)
    val fingerprint: String? get() = prefs.getString(FINGERPRINT, null)
    var serverId: String?
        get() = prefs.getString(SERVER_ID, null)
        set(value) { prefs.edit().putString(SERVER_ID, value).apply() }

    fun pairing(): Pairing? {
        val url = baseUrl ?: return null
        val fp = fingerprint ?: return null
        val sealed = prefs.getString(TOKEN, null) ?: return null
        return try { Pairing(url, fp, String(box.open(Base64.decode(sealed, Base64.NO_WRAP)), Charsets.UTF_8)) }
        catch (_: Exception) { null }
    }

    fun pair(pairing: Pairing) {
        val sealed = Base64.encodeToString(box.seal(pairing.token.toByteArray()), Base64.NO_WRAP)
        prefs.edit().putString(URL, pairing.baseUrl).putString(FINGERPRINT, pairing.fingerprint)
            .putString(TOKEN, sealed).remove(SERVER_ID).remove(NEEDS_REPAIR).commit()
    }

    /** Forgets the server. Every feature falls back to Syncthing; queued items stay in the spool. */
    fun unpair() {
        val edit = prefs.edit().remove(URL).remove(FINGERPRINT).remove(TOKEN).remove(SERVER_ID).remove(NEEDS_REPAIR)
        TransportFeature.entries.forEach { edit.remove(routeKey(it)) }
        edit.commit()
    }

    fun route(feature: TransportFeature): TransportRoute =
        if (paired && prefs.getString(routeKey(feature), null) == TransportRoute.HTTP.name) TransportRoute.HTTP
        else TransportRoute.FILES

    fun setRoute(feature: TransportFeature, route: TransportRoute) {
        require(route == TransportRoute.FILES || paired) { "먼저 서버와 페어링하세요" }
        prefs.edit().putString(routeKey(feature), route.name).commit()
    }

    val anyHttp: Boolean get() = TransportFeature.entries.any { route(it) == TransportRoute.HTTP }

    /** The server rejected the token (401) or the node (403): the user must pair again. */
    var needsRepair: Boolean
        get() = prefs.getBoolean(NEEDS_REPAIR, false)
        set(value) { prefs.edit().putBoolean(NEEDS_REPAIR, value).apply() }

    var lastSyncAt: Long
        get() = prefs.getLong(LAST_SYNC, 0L)
        set(value) { prefs.edit().putLong(LAST_SYNC, value).apply() }

    /** Stable code of the last failed run, or null after a success. Never a message or a path. */
    var lastError: String?
        get() = prefs.getString(LAST_ERROR, null)
        set(value) { prefs.edit().putString(LAST_ERROR, value).apply() }

    private fun routeKey(feature: TransportFeature) = "route.${feature.wire}"

    companion object {
        private const val URL = "url"
        private const val FINGERPRINT = "fingerprint"
        private const val TOKEN = "token"
        private const val SERVER_ID = "server_id"
        private const val NEEDS_REPAIR = "needs_repair"
        private const val LAST_SYNC = "last_sync_at"
        private const val LAST_ERROR = "last_error"

        fun of(context: Context): TransportSettings = TransportSettings(
            context.applicationContext.getSharedPreferences("transport", Context.MODE_PRIVATE), KeystoreSecretBox(),
        )
    }
}

/** AES-256-GCM with a non-exportable Android Keystore key; the IV is prepended to the ciphertext. */
class KeystoreSecretBox(private val alias: String = "weaver-transport-token") : SecretBox {
    private fun key(): javax.crypto.SecretKey {
        val store = java.security.KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? javax.crypto.SecretKey)?.let { return it }
        val generator = javax.crypto.KeyGenerator.getInstance(
            android.security.keystore.KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore",
        )
        generator.init(
            android.security.keystore.KeyGenParameterSpec.Builder(
                alias,
                android.security.keystore.KeyProperties.PURPOSE_ENCRYPT or android.security.keystore.KeyProperties.PURPOSE_DECRYPT,
            ).setBlockModes(android.security.keystore.KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(android.security.keystore.KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256).build(),
        )
        return generator.generateKey()
    }

    override fun seal(plain: ByteArray): ByteArray {
        val cipher = javax.crypto.Cipher.getInstance("AES/GCM/NoPadding").apply { init(javax.crypto.Cipher.ENCRYPT_MODE, key()) }
        return cipher.iv + cipher.doFinal(plain)
    }

    override fun open(sealed: ByteArray): ByteArray {
        val cipher = javax.crypto.Cipher.getInstance("AES/GCM/NoPadding").apply {
            init(javax.crypto.Cipher.DECRYPT_MODE, key(), javax.crypto.spec.GCMParameterSpec(128, sealed.copyOfRange(0, 12)))
        }
        return cipher.doFinal(sealed, 12, sealed.size - 12)
    }
}
