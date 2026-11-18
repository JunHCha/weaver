package io.github.junhcha.weaver.transport

import android.content.Context
import android.content.SharedPreferences
import android.util.Base64
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull

/**
 * Courier features whose write path the server can switch between Syncthing files and the Tailscale HTTP API.
 * [wire] is the server's `capabilities.transports` key. The server setting is the single source of truth: the phone
 * reads it and follows it, it never switches on its own.
 */
enum class TransportFeature(val wire: String, val label: String) {
    CAPTURE("capture", "수집"),
    VERDICT("review", "검토 판정"),
    ACTION_RESULT("action", "실행 결과"),
}

enum class TransportRoute(val label: String) { FILES("Syncthing"), HTTP("HTTP") }

/** Optional server features; while the server does not offer one the phone keeps reading its folder over files. */
enum class ServerFeature(val wire: String) { ALERTS("alerts"), PIPELINE("pipeline") }

/**
 * What the pairing QR carries (server `weaver mobile pair`). The phone pins the server's local CA, not the leaf,
 * so a re-issued server certificate needs no new pairing. [caDer] is the CA certificate, checked against [caSha256].
 */
class Pairing(
    val baseUrl: String, val serverId: String, val deviceId: String, val token: String,
    val caSha256: String, val caDer: ByteArray,
) {
    fun copy(baseUrl: String = this.baseUrl, token: String = this.token) =
        Pairing(baseUrl, serverId, deviceId, token, caSha256, caDer)
}

@Serializable
private data class PairingWire(
    val v: Int, val server_url: String, val server_id: String, val device_id: String, val token: String,
    val ca_sha256: String, val ca_der_b64: String,
)

/** Thrown for a QR/pasted pairing text that is not an acceptable v1 payload. Messages are shown to the user. */
class PairingException(message: String) : IllegalArgumentException(message)

object PairingCodec {
    private val json = Json { ignoreUnknownKeys = false }
    private val sha = Regex("[0-9a-f]{64}")
    private val tokenShape = Regex("[A-Za-z0-9_.~+/=-]{16,256}")
    private val idShape = Regex("[A-Za-z0-9_.:-]{1,128}")

    fun parse(text: String): Pairing {
        val wire = try { json.decodeFromString<PairingWire>(text.trim()) } catch (_: Exception) {
            throw PairingException("페어링 코드를 읽을 수 없습니다")
        }
        if (wire.v != 1) throw PairingException("지원하지 않는 페어링 버전입니다")
        val uri = try { java.net.URI(wire.server_url) } catch (_: Exception) { throw PairingException("서버 주소가 올바르지 않습니다") }
        if (uri.scheme != "https") throw PairingException("HTTPS 주소만 사용할 수 있습니다")
        if (uri.host.isNullOrEmpty() || uri.userInfo != null || uri.query != null || uri.fragment != null ||
            !(uri.path.isNullOrEmpty() || uri.path == "/")) throw PairingException("서버 주소가 올바르지 않습니다")
        if (!idShape.matches(wire.server_id) || !idShape.matches(wire.device_id)) throw PairingException("서버 정보가 올바르지 않습니다")
        if (!tokenShape.matches(wire.token)) throw PairingException("토큰 형식이 올바르지 않습니다")
        val fp = wire.ca_sha256.lowercase()
        if (!sha.matches(fp)) throw PairingException("CA 지문이 올바르지 않습니다")
        val der = try { java.util.Base64.getMimeDecoder().decode(wire.ca_der_b64.replace('-', '+').replace('_', '/')) }
        catch (_: Exception) { throw PairingException("CA 인증서를 읽을 수 없습니다") }
        val actual = java.security.MessageDigest.getInstance("SHA-256").digest(der).joinToString("") { "%02x".format(it) }
        if (actual != fp) throw PairingException("CA 인증서가 지문과 달라요")
        val ca = try {
            java.security.cert.CertificateFactory.getInstance("X.509").generateCertificate(der.inputStream()) as java.security.cert.X509Certificate
        } catch (_: Exception) { throw PairingException("CA 인증서를 읽을 수 없습니다") }
        if (ca.basicConstraints < 0) throw PairingException("CA 인증서가 아니에요")
        val port = if (uri.port > 0) ":${uri.port}" else ""
        return Pairing("https://${uri.host}$port", wire.server_id, wire.device_id, wire.token, fp, der)
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
    val serverId: String? get() = prefs.getString(SERVER_ID, null)
    val deviceId: String? get() = prefs.getString(DEVICE_ID, null)

    /**
     * The stored pairing, or null when it cannot be rebuilt. A paired phone whose token no longer opens (the Keystore
     * key was lost, e.g. after a restore or a lock-screen reset) is not "connected": it must pair again.
     */
    fun pairing(): Pairing? {
        val url = baseUrl ?: return null
        val sealed = prefs.getString(TOKEN, null) ?: return null
        return try {
            Pairing(
                url, serverId!!, deviceId!!, String(box.open(Base64.decode(sealed, Base64.NO_WRAP)), Charsets.UTF_8),
                prefs.getString(CA_SHA, null)!!, Base64.decode(prefs.getString(CA_DER, null)!!, Base64.NO_WRAP),
            )
        } catch (_: Exception) { needsRepair = true; null }
    }

    fun pair(pairing: Pairing) {
        val sealed = Base64.encodeToString(box.seal(pairing.token.toByteArray()), Base64.NO_WRAP)
        val edit = prefs.edit().putString(URL, pairing.baseUrl).putString(SERVER_ID, pairing.serverId)
            .putString(DEVICE_ID, pairing.deviceId).putString(CA_SHA, pairing.caSha256)
            .putString(CA_DER, Base64.encodeToString(pairing.caDer, Base64.NO_WRAP)).putString(TOKEN, sealed)
            .remove(NEEDS_REPAIR).remove(LAST_ERROR)
        // A different server starts with Syncthing routes until it announces its own.
        if (prefs.getString(SERVER_ID, null) != pairing.serverId) {
            TransportFeature.entries.forEach { edit.remove(routeKey(it)) }
            ServerFeature.entries.forEach { edit.remove(featureKey(it)) }
        }
        edit.commit()
    }

    /** Forgets the server. Every feature falls back to Syncthing; queued items stay and move there (see TransportRuntime). */
    fun unpair() {
        val edit = prefs.edit().remove(URL).remove(SERVER_ID).remove(DEVICE_ID).remove(CA_SHA).remove(CA_DER).remove(TOKEN)
            .remove(NEEDS_REPAIR).remove(LAST_ERROR)
        TransportFeature.entries.forEach { edit.remove(routeKey(it)) }
        ServerFeature.entries.forEach { edit.remove(featureKey(it)) }
        edit.commit()
    }

    /** The route the server announced for [feature]; Syncthing until the server says otherwise. */
    fun route(feature: TransportFeature): TransportRoute =
        if (paired && prefs.getString(routeKey(feature), null) == TransportRoute.HTTP.name) TransportRoute.HTTP
        else TransportRoute.FILES

    fun feature(feature: ServerFeature): Boolean = paired && prefs.getBoolean(featureKey(feature), false)

    /** Applies `capabilities.transports` and `capabilities.features`; returns the features whose route changed. */
    fun applyCapabilities(capabilities: JsonObject): List<TransportFeature> {
        val transports = capabilities["transports"] as? JsonObject
        val features = capabilities["features"] as? JsonObject
        val changed = mutableListOf<TransportFeature>()
        val edit = prefs.edit()
        TransportFeature.entries.forEach { f ->
            val wire = (transports?.get(f.wire) as? JsonPrimitive)?.contentOrNull
            val next = if (wire == "http") TransportRoute.HTTP else TransportRoute.FILES
            if (route(f) != next) changed += f
            edit.putString(routeKey(f), next.name)
        }
        ServerFeature.entries.forEach { f ->
            edit.putBoolean(featureKey(f), (features?.get(f.wire) as? JsonPrimitive)?.contentOrNull == "true")
        }
        edit.commit()
        return changed
    }

    val anyHttp: Boolean get() = TransportFeature.entries.any { route(it) == TransportRoute.HTTP }

    /** The server rejected the token (401) or the node (`node_mismatch`), or the token can no longer be read. */
    var needsRepair: Boolean
        get() = prefs.getBoolean(NEEDS_REPAIR, false)
        set(value) { prefs.edit().putBoolean(NEEDS_REPAIR, value).commit() }

    var lastSyncAt: Long
        get() = prefs.getLong(LAST_SYNC, 0L)
        set(value) { prefs.edit().putLong(LAST_SYNC, value).apply() }

    /** Stable code of the last failed run, or null after a success. Never a message or a path. */
    var lastError: String?
        get() = prefs.getString(LAST_ERROR, null)
        set(value) { prefs.edit().putString(LAST_ERROR, value).apply() }

    private fun routeKey(feature: TransportFeature) = "route.${feature.wire}"
    private fun featureKey(feature: ServerFeature) = "feature.${feature.wire}"

    companion object {
        private const val URL = "url"
        private const val DEVICE_ID = "device_id"
        private const val CA_SHA = "ca_sha256"
        private const val CA_DER = "ca_der"
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
