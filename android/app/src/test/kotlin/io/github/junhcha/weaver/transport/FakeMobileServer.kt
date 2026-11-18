package io.github.junhcha.weaver.transport

import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpsConfigurator
import com.sun.net.httpserver.HttpsServer
import java.io.File
import java.net.InetAddress
import java.net.InetSocketAddress
import java.security.KeyStore
import java.security.MessageDigest
import java.util.concurrent.Executors
import javax.net.ssl.KeyManagerFactory
import javax.net.ssl.SSLContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject

/**
 * A local, in-memory stand-in for the server's `/mobile/v1` API (ADR-040 contract) behind a real TLS socket with a
 * freshly generated self-signed certificate. Synthetic data only. Faults are injected per request path:
 * [dropResponse] processes the request and then closes the connection without answering (a lost response),
 * [fail] answers a fixed status instead.
 */
class FakeMobileServer(
    val serverId: String = SERVER_ID,
    val token: String = "t".repeat(43), leaf: String = "ip:127.0.0.1", val pki: TestPki = TestPki.default,
) : AutoCloseable {
    private val json = Json { ignoreUnknownKeys = true }
    private val server: HttpsServer
    val port: Int get() = server.address.port
    val url: String get() = "https://127.0.0.1:$port"

    // ---- state the tests read and set ----
    val payloads = mutableMapOf<String, ByteArray>()
    val receipts = mutableMapOf<String, String>()
    var payloadPuts = 0
    var manifestPuts = 0
    val reviews = mutableMapOf<Pair<String, Long>, Pair<String, String>>() // (id, rev) -> (json, etag)
    val verdicts = mutableMapOf<Pair<String, Long>, Pair<String, String>>() // (id, rev) -> (body, operation id)
    val operations = mutableMapOf<String, String>() // id -> state
    var verdictPuts = 0
    val alerts = linkedMapOf<String, String>()
    val acked = mutableListOf<String>()
    val actions = linkedMapOf<String, String>()
    val results = mutableMapOf<String, Pair<String, String>>()
    var resultPuts = 0
    var pipeline = listOf<String>()
    var threads = listOf<String>()
    var tags = listOf<String>()
    var wiki = listOf<String>()
    var dictionary = listOf<String>()
    var pageSize = 2
    val historyThreads = mutableSetOf<String>()
    val writes = mutableListOf<Triple<String, String, String>>() // method, path, body (applied once per key)
    val writeKeys = mutableListOf<String>()
    private val replayed = mutableMapOf<String, String>()
    /** Path suffixes whose response is lost after the request was fully processed (once each). */
    val dropResponse = mutableSetOf<String>()
    val fail = mutableMapOf<String, Int>()
    /** The Mac is off: connections are accepted and closed without an answer. */
    @Volatile var offline = false
    val seenAuth = mutableListOf<String?>()
    val requests = mutableListOf<String>()
    private var nextOp = 0
    var transports: Map<String, String> = emptyMap()
    var features: Map<String, Boolean> = emptyMap()
    /** Per path suffix: the fixed error code to answer with (default `injected_<status>`). */
    val failCodes = mutableMapOf<String, String>()
    var maxItemsPerResponse = Int.MAX_VALUE

    fun applyAll() { operations.keys.toList().forEach { operations[it] = "applied" } }

    init {
        val store = pki.leaf(leaf)
        val keys = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm()).apply { init(store, TestPki.PASSWORD.toCharArray()) }
        val context = SSLContext.getInstance("TLS").apply { init(keys.keyManagers, null, null) }
        server = HttpsServer.create(InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0)
        server.httpsConfigurator = HttpsConfigurator(context)
        server.executor = Executors.newCachedThreadPool()
        server.createContext("/") { exchange -> try { handle(exchange) } catch (error: Throwable) { exchange.close() } }
        server.start()
    }

    override fun close() = server.stop(0)

    // ---- request handling ----

    private fun reply(x: HttpExchange, status: Int, body: String = "{}", headers: Map<String, String> = emptyMap()) {
        val bytes = body.toByteArray()
        x.responseHeaders.add("Content-Type", "application/json")
        headers.forEach { (k, v) -> x.responseHeaders.add(k, v) }
        x.sendResponseHeaders(status, if (bytes.isEmpty()) -1 else bytes.size.toLong())
        if (bytes.isNotEmpty()) x.responseBody.use { it.write(bytes) } else x.close()
    }

    private fun error(x: HttpExchange, status: Int, code: String) =
        reply(x, status, """{"code":"$code","retryable":${status >= 500}}""")

    private fun handle(x: HttpExchange) {
        if (offline) { x.close(); return }
        val full = x.requestURI.path
        val query = x.requestURI.rawQuery.orEmpty()
        val method = x.requestMethod
        requests.add("$method $full")
        val auth = x.requestHeaders.getFirst("Authorization")
        seenAuth.add(auth)
        val body = x.requestBody.readBytes()
        if (auth != "Bearer $token") return error(x, 401, "token_invalid")
        if (!full.startsWith("/mobile/v1")) return error(x, 404, "not_found")
        val path = full.removePrefix("/mobile/v1")
        fail.entries.firstOrNull { path.endsWith(it.key) }?.let { return error(x, it.value, failCodes[it.key] ?: "injected_${it.value}") }
        val drop = dropResponse.firstOrNull { path.endsWith(it) }
        val respond: (Int, String, Map<String, String>) -> Unit = { status, text, headers ->
            if (drop != null) { dropResponse.remove(drop); x.close() } else reply(x, status, text, headers)
        }
        route(x, method, path, query, body, respond)
    }

    private fun page(items: List<String>, query: String, respond: (Int, String, Map<String, String>) -> Unit) {
        val start = Regex("cursor=([0-9]+)").find(query)?.groupValues?.get(1)?.toInt() ?: 0
        val limit = Regex("limit=([0-9]+)").find(query)?.groupValues?.get(1)?.toInt() ?: Int.MAX_VALUE
        val size = minOf(pageSize, limit, maxItemsPerResponse)
        val slice = items.drop(start).take(size)
        val body = """{"items":[${slice.joinToString(",")}],"next_cursor":${if (start + size < items.size) "\"${start + size}\"" else "null"}}"""
        // The phone only accepts 4 MiB: answer with an oversized body so the client's own limit trips.
        respond(200, body, emptyMap())
    }

    private fun route(x: HttpExchange, method: String, path: String, query: String, body: ByteArray, respond: (Int, String, Map<String, String>) -> Unit) {
        val parts = path.trim('/').split('/')
        val none = emptyMap<String, String>()
        fun err(status: Int, code: String) = respond(status, """{"code":"$code","retryable":${status >= 500}}""", none)
        when {
            method == "GET" && path == "/capabilities" -> respond(200, """{"server_id":"$serverId","transport_version":1,"transports":{${
                transports.entries.joinToString(",") { "\"${it.key}\":\"${it.value}\"" }}},"features":{${
                features.entries.joinToString(",") { "\"${it.key}\":${it.value}" }}}}""", none)
            parts[0] == "captures" && parts.size == 3 -> {
                val id = parts[1]
                when {
                    method == "PUT" && parts[2] == "payload" -> {
                        payloadPuts++
                        val sha = MessageDigest.getInstance("SHA-256").digest(body).joinToString("") { "%02x".format(it) }
                        if (sha != x.requestHeaders.getFirst("X-Content-SHA256")) return err(422, "hash_mismatch")
                        val existing = payloads[id]
                        if (existing != null && !existing.contentEquals(body)) return err(409, "capture_conflict")
                        payloads[id] = body
                        respond(if (existing == null) 201 else 200, "{}", none)
                    }
                    method == "PUT" && parts[2] == "manifest" -> {
                        manifestPuts++
                        val manifest = json.parseToJsonElement(body.decodeToString()).jsonObject
                        val payload = payloads[id] ?: return err(409, "payload_missing")
                        val sha = (manifest["sha256"] as JsonPrimitive).content
                        val real = MessageDigest.getInstance("SHA-256").digest(payload).joinToString("") { "%02x".format(it) }
                        if (sha != real) return err(422, "hash_mismatch")
                        val first = receipts[id] == null
                        val receipt = receipts.getOrPut(id) {
                            """{"schema_version":1,"capture_id":"$id","source_id":"src_01ARZ3NDEKTSV4RRFFQ69G5FAV","sha256":"$sha","received_at":"2026-11-19T00:00:00Z"}"""
                        }
                        respond(if (first) 201 else 200, receipt, none)
                    }
                    method == "GET" && parts[2] == "receipt" -> receipts[id]?.let { respond(200, it, none) } ?: err(404, "not_found")
                    else -> err(404, "not_found")
                }
            }
            method == "GET" && path == "/reviews" -> respond(200, """{"items":[${
                reviews.keys.groupBy { it.first }.map { (id, keys) -> keys.maxBy { it.second } }
                    .joinToString(",") { """{"package_id":"${it.first}","revision":${it.second}}""" }}]}""", none)
            parts[0] == "reviews" && parts.size == 4 && method == "GET" -> {
                val (doc, etag) = reviews[parts[1] to parts[3].toLong()] ?: return err(404, "not_found")
                respond(200, doc, mapOf("ETag" to etag))
            }
            parts[0] == "reviews" && parts.size == 5 && method == "PUT" -> {
                verdictPuts++
                val key = parts[1] to parts[3].toLong()
                val text0 = body.decodeToString()
                // Same key and content: the first operation, even after the revision moved on or the verdict was applied.
                verdicts[key]?.takeIf { it.first == text0 }?.let { return respond(202, """{"operation_id":"${it.second}"}""", none) }
                val current = reviews[key]
                val latest = reviews.keys.filter { it.first == key.first }.maxOfOrNull { it.second }
                if (current == null || latest != key.second || x.requestHeaders.getFirst("If-Match") != current.second) {
                    return respond(412, """{"code":"stale_revision","retryable":false,"current_revision":${latest ?: 0}}""", none)
                }
                val text = body.decodeToString()
                val existing = verdicts[key]
                if (existing != null && existing.first != text) return err(409, "verdict_conflict")
                val op = existing?.second ?: "op_${++nextOp}".also { operations[it] = "queued" }
                verdicts[key] = text to op
                respond(202, """{"operation_id":"$op"}""", none)
            }
            parts[0] == "operations" && method == "GET" ->
                operations[parts[1]]?.let { respond(200, """{"operation_id":"${parts[1]}","state":"$it"}""", none) } ?: err(404, "not_found")
            method == "GET" && path == "/alerts" -> respond(200, """{"items":[${alerts.values.joinToString(",")}]}""", none)
            parts[0] == "alerts" && parts.size == 3 && method == "PUT" -> { acked.add(parts[1]); alerts.remove(parts[1]); respond(200, "{}", none) }
            method == "GET" && path == "/actions" -> respond(200, """{"items":[${actions.values.joinToString(",")}]}""", none)
            parts[0] == "actions" && parts.size == 3 && method == "PUT" -> {
                resultPuts++
                val text = body.decodeToString()
                val existing = results[parts[1]]
                if (existing != null && existing.first != text) return err(409, "result_conflict")
                val op = existing?.second ?: "op_${++nextOp}".also { operations[it] = "queued" }
                results[parts[1]] = text to op
                respond(202, """{"operation_id":"$op"}""", none)
            }
            method == "GET" && path == "/pipeline" -> page(pipeline, query, respond)
            method == "GET" && path == "/fabric/threads" -> {
                if (query.contains("cursor=${failThreadsFrom}") && failThreadsFrom != null) return err(503, "unavailable")
                page(threads, query, respond)
            }
            method == "GET" && path == "/fabric/tags" -> respond(200, """{"items":[${tags.joinToString(",")}]}""", none)
            method == "GET" && path == "/wiki/pages" -> page(wiki, query, respond)
            method == "GET" && path == "/dictionary" -> respond(200, """{"items":[${dictionary.joinToString(",")}]}""", none)
            method in setOf("POST", "PATCH", "DELETE") && (parts[0] == "threads" || parts[0] == "dictionary") -> {
                val key = x.requestHeaders.getFirst("Idempotency-Key") ?: return err(422, "idempotency_key_required")
                replayed[key]?.let { return respond(200, it, none) }
                val text = body.decodeToString()
                if (parts[0] == "threads" && parts.getOrNull(1) in historyThreads) return err(422, "history_thread")
                writes.add(Triple(method, path, text)); writeKeys.add(key)
                val answer = if (path == "/threads/tags/batch") """{"changed":${
                    (json.parseToJsonElement(text).jsonObject["thread_ids"] as kotlinx.serialization.json.JsonArray).size},"skipped":0}"""
                else """{"ok":true}"""
                replayed[key] = answer
                respond(200, answer, none)
            }
            else -> err(404, "not_found")
        }
    }

    var failThreadsFrom: String? = null

    companion object {
        const val SERVER_ID = "srv-test"
        fun pairing(server: FakeMobileServer, pki: TestPki = server.pki, token: String = server.token) =
            Pairing(server.url, server.serverId, "dev-1", token, pki.caSha256, pki.caDer)

        /** The QR payload the server prints, as the phone parses it. */
        fun code(server: FakeMobileServer, pki: TestPki = server.pki, token: String = server.token, serverId: String = server.serverId, caDer: ByteArray = pki.caDer, caSha: String = pki.caSha256) =
            """{"v":1,"server_url":"${server.url}","server_id":"$serverId","device_id":"dev-1","token":"$token","ca_sha256":"$caSha","ca_der_b64":"${java.util.Base64.getEncoder().encodeToString(caDer)}"}"""
    }
}

/** A throwaway CA and the leaf certificates it issues (keytool, synthetic names). */
class TestPki private constructor(private val dir: File) {
    private val keytool = File(System.getProperty("java.home"), "bin/keytool").absolutePath
    private val caStore = File(dir, "ca.p12")
    private val leaves = mutableMapOf<String, KeyStore>()

    private fun run(vararg args: String) {
        val process = ProcessBuilder(listOf(keytool) + args).redirectErrorStream(true).start()
        check(process.waitFor() == 0) { process.inputStream.readBytes().decodeToString() }
    }

    init {
        run("-genkeypair", "-alias", "ca", "-keyalg", "RSA", "-keysize", "2048", "-validity", "30", "-dname", "CN=weaver-test-ca",
            "-ext", "bc:c", "-ext", "ku:c=keyCertSign,cRLSign", "-keystore", caStore.path, "-storetype", "PKCS12",
            "-storepass", PASSWORD, "-keypass", PASSWORD)
        run("-exportcert", "-alias", "ca", "-keystore", caStore.path, "-storepass", PASSWORD, "-rfc", "-file", File(dir, "ca.pem").path)
    }

    val caDer: ByteArray by lazy {
        java.security.cert.CertificateFactory.getInstance("X.509").generateCertificate(File(dir, "ca.pem").inputStream()).encoded
    }
    val caSha256: String by lazy { MessageDigest.getInstance("SHA-256").digest(caDer).joinToString("") { "%02x".format(it) } }

    /** A leaf signed by this CA with the given SAN extension (e.g. `ip:127.0.0.1`), or none when [san] is blank. */
    fun leaf(san: String): KeyStore = leaves.getOrPut(san) {
        val tag = "leaf${leaves.size}"
        val store = File(dir, "$tag.p12")
        val sanArgs = if (san.isBlank()) emptyArray() else arrayOf("-ext", "san=$san")
        run(*(arrayOf("-genkeypair", "-alias", "server", "-keyalg", "RSA", "-keysize", "2048", "-validity", "30", "-dname", "CN=weaver-test",
            "-keystore", store.path, "-storetype", "PKCS12", "-storepass", PASSWORD, "-keypass", PASSWORD) + sanArgs))
        run("-certreq", "-alias", "server", "-keystore", store.path, "-storepass", PASSWORD, "-file", File(dir, "$tag.csr").path)
        run(*(arrayOf("-gencert", "-alias", "ca", "-keystore", caStore.path, "-storepass", PASSWORD, "-infile", File(dir, "$tag.csr").path,
            "-outfile", File(dir, "$tag.pem").path, "-rfc", "-validity", "30", "-ext", "eku=sa") + sanArgs))
        run("-importcert", "-alias", "ca", "-file", File(dir, "ca.pem").path, "-keystore", store.path, "-storepass", PASSWORD, "-noprompt")
        run("-importcert", "-alias", "server", "-file", File(dir, "$tag.pem").path, "-keystore", store.path, "-storepass", PASSWORD, "-noprompt")
        KeyStore.getInstance("PKCS12").apply { store.inputStream().use { load(it, PASSWORD.toCharArray()) } }
    }

    companion object {
        const val PASSWORD = "changeit"
        val default: TestPki by lazy { create() }
        fun create() = TestPki(java.nio.file.Files.createTempDirectory("weaver-test-pki").toFile())
    }
}
