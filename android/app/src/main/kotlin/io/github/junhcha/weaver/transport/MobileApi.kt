package io.github.junhcha.weaver.transport

import java.io.File
import java.io.IOException
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import javax.net.ssl.HostnameVerifier
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject

/** The server answered with an error. [code] is the server's fixed error code; nothing else is ever logged. */
class ApiException(val status: Int, val code: String, val retryable: Boolean, val retryAfterSeconds: Long? = null) :
    IOException("api_$status")

/** The server could not be reached (VPN off, Mac asleep, DNS, timeout) or answered an unreadable body. */
class UnreachableException(cause: Throwable? = null) : IOException("unreachable", cause)

/** The certificate did not match the fingerprint from pairing. Never retried, never downgraded to plain HTTP. */
class PinMismatchException : IOException("pin_mismatch")

data class Versioned(val body: ByteArray, val etag: String?)
data class ReviewRef(val packageId: String, val revision: Long)
data class Operation(val id: String, val state: String, val code: String?) {
    val terminal: Boolean get() = state in setOf("applied", "failed", "stale")
}
data class Page(val items: List<JsonElement>, val nextCursor: String?)

/**
 * Reads every page of a cursor list. A page that exceeds the response limit is not a failure: the same cursor is asked
 * again with half the page size (down to [MIN_LIMIT]), and the smaller size is kept for the rest of the list.
 */
object Paging {
    const val MIN_LIMIT = 5
    const val MAX_PAGES = 5000

    fun all(startLimit: Int, fetch: (cursor: String?, limit: Int) -> Page): List<JsonElement> {
        val items = mutableListOf<JsonElement>()
        var cursor: String? = null
        var limit = startLimit
        var pages = 0
        while (true) {
            val page = try { fetch(cursor, limit) } catch (error: ApiException) {
                if (error.code == "response_too_large" && limit > MIN_LIMIT) { limit = maxOf(MIN_LIMIT, limit / 2); continue }
                throw error
            }
            items += page.items
            cursor = page.nextCursor
            check(++pages <= MAX_PAGES) { "too_many_pages" }
            if (cursor == null) break
        }
        return items
    }
}

/**
 * The phone side of the `/mobile/v1` contract (ADR-040). Every method is blocking and runs on an IO thread.
 * A 2xx for a write only means the step it names (stored, accepted); callers keep their copy until the
 * server's own confirmation (Receipt, operation `applied`).
 */
interface MobileApi {
    fun capabilities(): JsonObject
    /** 200/201 once the server staged the payload for [captureId]; [file] is streamed, never held in memory. */
    fun putPayload(captureId: String, file: File, sha256: String)
    /** The Receipt v1 JSON the server persisted for this manifest. */
    fun putManifest(captureId: String, manifest: ByteArray): ByteArray
    fun receipt(captureId: String): ByteArray?
    fun reviews(): List<ReviewRef>
    fun review(packageId: String, revision: Long): Versioned
    fun putVerdict(packageId: String, revision: Long, verdict: ByteArray, etag: String): String
    fun operation(id: String): Operation
    fun alerts(): List<JsonElement>
    fun ackAlert(alertId: String)
    fun actions(): List<JsonElement>
    fun putActionResult(actionId: String, result: ByteArray): String
    fun pipeline(cursor: String?, limit: Int): Page
    fun threads(cursor: String?, limit: Int): Page
    fun tags(): List<JsonElement>
    fun wikiPages(cursor: String?, limit: Int): Page
    fun dictionary(): List<JsonElement>
    /** Idempotent write: the same [key] and body return the first answer. Returns the response JSON. */
    fun write(method: String, path: String, body: JsonObject?, key: String): JsonElement
}

/**
 * HTTPS only. The server certificate must chain to the CA from the pairing QR (standard PKIX validation: signature,
 * validity, server-auth usage) and its subject alternative names must contain the server address, so a re-issued
 * leaf keeps working and any other certificate is refused. No system CA, no plaintext fallback. Redirects are
 * never followed, so the bearer token cannot reach another host.
 */
class HttpsMobileApi(
    private val pairing: Pairing,
    private val connectTimeoutMs: Int = 8_000,
    private val readTimeoutMs: Int = 30_000,
) : MobileApi {
    init { require(pairing.baseUrl.startsWith("https://")) { "https_required" } }

    private val json = Json { ignoreUnknownKeys = true }
    private val sslContext: SSLContext = SSLContext.getInstance("TLS").apply {
        init(null, arrayOf(CaTrustManager(pairing.caDer)), null)
    }
    @Volatile private var hostRefused = false
    private val hostCheck = HostnameVerifier { host, session -> SanMatch.matches(host, session).also { if (!it) hostRefused = true } }

    private class Reply(val status: Int, val headers: Map<String, String>, val body: ByteArray)

    private fun open(method: String, path: String, headers: Map<String, String>): HttpsURLConnection {
        val connection = URL(pairing.baseUrl + BASE + path).openConnection() as HttpsURLConnection
        connection.sslSocketFactory = sslContext.socketFactory
        connection.hostnameVerifier = hostCheck
        connection.instanceFollowRedirects = false
        connection.connectTimeout = connectTimeoutMs
        connection.readTimeout = readTimeoutMs
        connection.useCaches = false
        connection.requestMethod = method
        connection.setRequestProperty("Authorization", "Bearer ${pairing.token}")
        connection.setRequestProperty("Accept", "application/json")
        headers.forEach { (name, value) -> connection.setRequestProperty(name, value) }
        return connection
    }

    private fun call(
        method: String, path: String, headers: Map<String, String> = emptyMap(), body: ByteArray? = null,
        file: File? = null, limit: Int = MAX_RESPONSE,
    ): Reply {
        val connection = open(method, path, headers)
        try {
            when {
                file != null -> {
                    connection.doOutput = true
                    connection.setFixedLengthStreamingMode(file.length())
                    connection.outputStream.use { out -> file.inputStream().use { it.copyTo(out) } }
                }
                body != null -> {
                    connection.doOutput = true
                    connection.setFixedLengthStreamingMode(body.size)
                    connection.outputStream.use { it.write(body) }
                }
            }
            val status = connection.responseCode
            if (status in 300..399) throw ApiException(status, "redirect_refused", false)
            val stream = if (status >= 400) connection.errorStream else connection.inputStream
            val bytes = stream?.use { readLimited(it, limit) } ?: ByteArray(0)
            val reply = Reply(status, connection.headerFields.filterKeys { it != null }
                .mapKeys { it.key.lowercase() }.mapValues { it.value.last() }, bytes)
            if (status >= 400) throw error(reply)
            return reply
        } catch (error: ApiException) { throw error
        } catch (error: javax.net.ssl.SSLPeerUnverifiedException) { throw PinMismatchException()
        } catch (error: javax.net.ssl.SSLException) {
            throw if (generateSequence<Throwable>(error) { it.cause }.any { it is PinMismatchException }) PinMismatchException()
            else UnreachableException(error)
        } catch (error: IOException) { throw if (hostRefused) PinMismatchException() else UnreachableException(error)
        } finally { connection.disconnect() }
    }

    private fun error(reply: Reply): ApiException {
        val parsed = runCatching { json.parseToJsonElement(reply.body.decodeToString()).jsonObject }.getOrNull()
        val code = (parsed?.get("code") as? JsonPrimitive)?.contentOrNull?.takeIf { it.matches(Regex("[a-z0-9_]{1,64}")) }
            ?: "http_${reply.status}"
        val retryable = (parsed?.get("retryable") as? JsonPrimitive)?.contentOrNull?.toBooleanStrictOrNull()
            ?: (reply.status == 429 || reply.status >= 500)
        return ApiException(reply.status, code, retryable, reply.headers["retry-after"]?.toLongOrNull())
    }

    private fun readLimited(input: InputStream, limit: Int): ByteArray {
        val out = java.io.ByteArrayOutputStream()
        val buffer = ByteArray(8192)
        while (true) {
            val count = input.read(buffer)
            if (count < 0) break
            out.write(buffer, 0, count)
            if (out.size() > limit) throw ApiException(200, "response_too_large", false)
        }
        return out.toByteArray()
    }

    private fun obj(reply: Reply): JsonObject = try { json.parseToJsonElement(reply.body.decodeToString()).jsonObject }
    catch (_: Exception) { throw UnreachableException() }

    private fun items(reply: Reply): List<JsonElement> =
        (obj(reply)["items"] as? JsonArray)?.toList() ?: throw UnreachableException()

    private fun page(reply: Reply): Page {
        val root = obj(reply)
        return Page((root["items"] as? JsonArray)?.toList() ?: throw UnreachableException(),
            (root["next_cursor"] as? JsonPrimitive)?.contentOrNull)
    }

    private fun cursor(path: String, cursor: String?, limit: Int) =
        if (cursor == null) "$path?limit=$limit" else "$path?limit=$limit&cursor=" + java.net.URLEncoder.encode(cursor, "UTF-8")

    override fun capabilities() = obj(call("GET", "/capabilities"))

    override fun putPayload(captureId: String, file: File, sha256: String) {
        call("PUT", "/captures/$captureId/payload", mapOf("Content-Type" to "application/octet-stream",
            "X-Content-SHA256" to sha256), file = file)
    }

    override fun putManifest(captureId: String, manifest: ByteArray) =
        call("PUT", "/captures/$captureId/manifest", mapOf("Content-Type" to "application/json"), manifest).body

    override fun receipt(captureId: String): ByteArray? = try { call("GET", "/captures/$captureId/receipt").body }
    catch (error: ApiException) { if (error.status == 404) null else throw error }

    override fun reviews() = items(call("GET", "/reviews")).map {
        val o = it.jsonObject
        ReviewRef((o["package_id"] as JsonPrimitive).content, (o["revision"] as JsonPrimitive).content.toLong())
    }

    override fun review(packageId: String, revision: Long): Versioned {
        val reply = call("GET", "/reviews/$packageId/revisions/$revision", limit = MAX_DOCUMENT)
        return Versioned(reply.body, reply.headers["etag"])
    }

    override fun putVerdict(packageId: String, revision: Long, verdict: ByteArray, etag: String): String =
        operationId(call("PUT", "/reviews/$packageId/revisions/$revision/verdict",
            mapOf("Content-Type" to "application/json", "If-Match" to etag), verdict))

    override fun operation(id: String): Operation {
        val o = obj(call("GET", "/operations/$id"))
        return Operation(id, (o["state"] as? JsonPrimitive)?.content ?: throw UnreachableException(),
            (o["code"] as? JsonPrimitive)?.contentOrNull)
    }

    override fun alerts() = items(call("GET", "/alerts"))
    override fun ackAlert(alertId: String) { call("PUT", "/alerts/$alertId/ack") }
    override fun actions() = items(call("GET", "/actions"))

    override fun putActionResult(actionId: String, result: ByteArray): String =
        operationId(call("PUT", "/actions/$actionId/result", mapOf("Content-Type" to "application/json"), result))

    override fun pipeline(cursor: String?, limit: Int) = page(call("GET", cursor("/pipeline", cursor, limit), limit = MAX_PAGE))
    override fun threads(cursor: String?, limit: Int) = page(call("GET", cursor("/fabric/threads", cursor, limit), limit = MAX_PAGE))
    override fun tags() = items(call("GET", "/fabric/tags", limit = MAX_PAGE))
    override fun wikiPages(cursor: String?, limit: Int) = page(call("GET", cursor("/wiki/pages", cursor, limit), limit = MAX_PAGE))
    override fun dictionary() = items(call("GET", "/dictionary", limit = MAX_PAGE))

    override fun write(method: String, path: String, body: JsonObject?, key: String): JsonElement {
        val reply = call(method, path, mapOf("Content-Type" to "application/json", "Idempotency-Key" to key),
            body?.toString()?.toByteArray() ?: "{}".toByteArray())
        return runCatching { json.parseToJsonElement(reply.body.decodeToString()) }.getOrDefault(JsonObject(emptyMap()))
    }

    private fun operationId(reply: Reply): String =
        (obj(reply)["operation_id"] as? JsonPrimitive)?.contentOrNull ?: throw UnreachableException()

    companion object {
        const val BASE = "/mobile/v1"
        const val PAGE = 500
        const val MAX_RESPONSE = 4 shl 20
        const val MAX_DOCUMENT = 1 shl 20
        const val MAX_PAGE = 4 shl 20
    }
}

/** Validates the chain against only the paired CA; any failure surfaces as [PinMismatchException]. */
internal class CaTrustManager(caDer: ByteArray) : X509TrustManager {
    private val delegate: X509TrustManager = run {
        val ca = java.security.cert.CertificateFactory.getInstance("X.509").generateCertificate(caDer.inputStream())
        val store = java.security.KeyStore.getInstance(java.security.KeyStore.getDefaultType()).apply {
            load(null); setCertificateEntry("weaver-ca", ca)
        }
        val factory = javax.net.ssl.TrustManagerFactory.getInstance(javax.net.ssl.TrustManagerFactory.getDefaultAlgorithm())
        factory.init(store)
        factory.trustManagers.filterIsInstance<X509TrustManager>().first()
    }
    override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) =
        throw CertificateException("client_auth_unsupported")
    override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        try { delegate.checkServerTrusted(chain, authType) } catch (error: CertificateException) { throw CertificateException(PinMismatchException()) }
    }
    override fun getAcceptedIssuers(): Array<X509Certificate> = delegate.acceptedIssuers
}

/** Hostname check against the leaf's subject alternative names (IP entries compare as addresses, DNS names as text). */
internal object SanMatch {
    fun matches(host: String, session: javax.net.ssl.SSLSession): Boolean {
        val leaf = session.peerCertificates.firstOrNull() as? X509Certificate ?: return false
        val names = leaf.subjectAlternativeNames ?: return false
        val bare = host.removePrefix("[").removeSuffix("]")
        val literal = bare.all { it.isDigit() || it == '.' || it == ':' || it in 'a'..'f' || it in 'A'..'F' }
        val address = if (literal) runCatching { java.net.InetAddress.getByName(bare) }.getOrNull() else null
        return names.any { entry ->
            val value = entry[1] as? String ?: return@any false
            when (entry[0] as Int) {
                7 -> address != null && runCatching { java.net.InetAddress.getByName(value) == address }.getOrDefault(false)
                2 -> value.equals(bare, ignoreCase = true) ||
                    (value.startsWith("*.") && bare.substringAfter('.', "").equals(value.drop(2), ignoreCase = true))
                else -> false
            }
        }
    }
}
