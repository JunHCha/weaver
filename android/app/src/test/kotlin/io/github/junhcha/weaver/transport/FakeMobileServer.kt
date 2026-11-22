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
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject

/** One recorded case of `schema/mobile/fixtures/<group>.json`: the request the phone must send and the exact response. */
class FixtureCase(val key: String, raw: JsonObject) {
    val method = (raw["request"]!!.jsonObject["method"] as JsonPrimitive).content
    val path = (raw["request"]!!.jsonObject["path"] as JsonPrimitive).content
    val query: Map<String, String> = (raw["request"]!!.jsonObject["query"] as? JsonObject)?.mapValues { (it.value as JsonPrimitive).content }.orEmpty()
    val requestHeaders: Map<String, String> = (raw["request"]!!.jsonObject["headers"] as? JsonObject)
        ?.mapValues { (it.value as JsonPrimitive).content }.orEmpty()
    val requestBody: JsonElement? = raw["request"]!!.jsonObject["body"]
    val requestText: String? = (raw["request"]!!.jsonObject["body_text"] as? JsonPrimitive)?.content
    val status = (raw["response"]!!.jsonObject["status"] as JsonPrimitive).content.toInt()
    val responseHeaders: Map<String, String> = (raw["response"]!!.jsonObject["headers"] as? JsonObject)
        ?.mapValues { (it.value as JsonPrimitive).content }.orEmpty()
    val responseBody: JsonElement? = raw["response"]!!.jsonObject["body"]
}

/** The contract fixtures the server tests generate and validate; byte-identical copies live under schema/mobile/fixtures. */
object ContractFixtures {
    private val dir = File(requireNotNull(System.getProperty("weaver.mobileFixtureDir")) { "weaver.mobileFixtureDir is not set" })
    val cases: Map<String, FixtureCase> by lazy {
        val json = Json { ignoreUnknownKeys = true }
        dir.listFiles { f -> f.extension == "json" }!!.sortedBy { it.name }.flatMap { file ->
            val root = json.parseToJsonElement(file.readText()).jsonObject
            val group = (root["group"] as JsonPrimitive).content
            (root["cases"] as kotlinx.serialization.json.JsonArray).mapNotNull { c ->
                val o = c.jsonObject
                if (o["request"] == null) null else "$group.${(o["name"] as JsonPrimitive).content}".let { it to FixtureCase(it, o) }
            }
        }.toMap()
    }
    operator fun get(name: String): FixtureCase = cases[name] ?: error("no fixture case $name")
    fun raw(group: String): JsonObject = Json.parseToJsonElement(File(dir, "$group.json").readText()).jsonObject
    /** The pairing QR payload the server prints, with the CA material of the test PKI filled in. */
    fun pairingPayload(): JsonObject = (raw("pairing")["cases"] as kotlinx.serialization.json.JsonArray)[0].jsonObject["payload"]!!.jsonObject
}

/**
 * A local TLS server that answers **only with fixture responses**: tests script which cases it serves
 * (`serve("reviews.list", "reviews.get_revision")`), and every request is checked against the fixture's request (method,
 * path, cursor, content headers, JSON/text body). A request that does not match any scripted case, or differs from the
 * fixture's request, is answered 404/400 and recorded in [mismatches]. Faults are injected per request: [dropResponse] processes
 * the request and then closes the connection without answering (a lost response), [failWith] answers a fixture error case,
 * [offline] closes every connection (the Mac is off). Synthetic data only.
 */
class FakeMobileServer(
    leaf: String = "ip:127.0.0.1", val pki: TestPki = TestPki.default,
) : AutoCloseable {
    private val json = Json { ignoreUnknownKeys = true }
    private val server: HttpsServer
    val port: Int get() = server.address.port
    val url: String get() = "https://127.0.0.1:$port"
    var serverId: String = ((ContractFixtures["capabilities.capabilities"].responseBody as JsonObject)["server_id"] as JsonPrimitive).content
    val token: String = (ContractFixtures.pairingPayload()["token"] as JsonPrimitive).content

    private val scripted = linkedMapOf<String, MutableList<FixtureCase>>()
    private val consumed = mutableMapOf<String, Int>()
    private val lastPage = mutableMapOf<String, Int>()
    private val failing = mutableMapOf<String, FixtureCase>()
    /** Path suffixes whose response is lost once after the request was processed. */
    val dropResponse = mutableSetOf<String>()
    @Volatile var offline = false
    /** Every request as `METHOD path` (without the query). */
    val requests = java.util.Collections.synchronizedList(mutableListOf<String>())
    val bodies = java.util.Collections.synchronizedList(mutableListOf<Pair<String, String>>())
    val mismatches = java.util.Collections.synchronizedList(mutableListOf<String>())
    val seenAuth = java.util.Collections.synchronizedList(mutableListOf<String?>())

    private fun route(case: FixtureCase) = "${case.method} ${case.path}"

    /** Script cases ("group.name"); same method+path cases answer in order, the last one repeats. */
    fun serve(vararg names: String) = names.forEach { n -> ContractFixtures[n].let { scripted.getOrPut(route(it)) { mutableListOf() }.add(it) } }
    /** A fixture case whose response body is changed by [change] (e.g. one thread with an unknown kind); still derived, not invented. */
    fun serveDerived(name: String, change: (JsonElement) -> JsonElement) {
        val base = ContractFixtures[name]
        val raw = JsonObject(mapOf(
            "request" to JsonObject(mapOf("method" to JsonPrimitive(base.method), "path" to JsonPrimitive(base.path),
                "query" to JsonObject(base.query.mapValues { JsonPrimitive(it.value) }),
                "headers" to JsonObject(base.requestHeaders.mapValues { JsonPrimitive(it.value) }))),
            "response" to JsonObject(mapOf("status" to JsonPrimitive(base.status), "headers" to JsonObject(base.responseHeaders.mapValues { JsonPrimitive(it.value) }),
                "body" to change(base.responseBody!!)))))
        val case = FixtureCase(name, raw)
        scripted.getOrPut(route(case)) { mutableListOf() }.add(case)
    }

    /** Forget scripted answers of a route (for tests that re-script it). */
    fun reset(vararg names: String) = names.forEach { n -> ContractFixtures[n].let { scripted.remove(route(it)); consumed.remove(route(it)); lastPage.remove(route(it)) } }
    /** Answer every request whose path ends with [suffix] with the fixture error [name] (e.g. "errors.scope_denied"). */
    fun failWith(suffix: String, name: String) { failing[suffix] = ContractFixtures[name] }
    fun clearFailures() = failing.clear()
    fun count(substring: String) = requests.count { it.contains(substring) }

    /** `capabilities.capabilities` with the server's transports/features changed (the server config is what changes them). */
    private var capabilityOverride: JsonObject? = null
    fun capabilities(transports: Map<String, String>? = null, features: Map<String, Boolean>? = null, serverId: String? = null) {
        val base = (capabilityOverride ?: ContractFixtures["capabilities.capabilities"].responseBody) as JsonObject
        if (serverId != null) this.serverId = serverId
        capabilityOverride = JsonObject(base.toMutableMap().also { m ->
            if (serverId != null) m["server_id"] = JsonPrimitive(serverId)
            if (transports != null) m["transports"] = JsonObject((base["transports"] as JsonObject).toMutableMap().also { t -> transports.forEach { (k, v) -> t[k] = JsonPrimitive(v) } })
            if (features != null) m["features"] = JsonObject((base["features"] as JsonObject).toMutableMap().also { t -> features.forEach { (k, v) -> t[k] = JsonPrimitive(v) } })
        })
    }

    /** Serve a whole capabilities fixture case (e.g. `pipeline.enabled_capabilities`) instead of the default one. */
    fun capabilitiesFrom(name: String) { capabilityOverride = ContractFixtures[name].responseBody as JsonObject }

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

    private fun queryOf(raw: String?): Map<String, String> = raw.orEmpty().split('&').filter { it.isNotEmpty() }.associate {
        val (k, v) = (it.split('=', limit = 2) + "").take(2)
        java.net.URLDecoder.decode(k, "UTF-8") to java.net.URLDecoder.decode(v, "UTF-8")
    }

    private fun reply(x: HttpExchange, case: FixtureCase) {
        val body = case.responseBody?.toString()?.toByteArray()
        case.responseHeaders.forEach { (k, v) -> x.responseHeaders.add(k, v) }
        if (body != null) x.responseHeaders.add("Content-Type", "application/json")
        if (body == null) { x.sendResponseHeaders(case.status, -1); x.close() }
        else { x.sendResponseHeaders(case.status, body.size.toLong()); x.responseBody.use { it.write(body) } }
    }

    private fun fixtureError(x: HttpExchange, name: String) = reply(x, ContractFixtures[name])

    private fun handle(x: HttpExchange) {
        if (offline) { x.close(); return }
        val path = x.requestURI.path
        val method = x.requestMethod
        val query = queryOf(x.requestURI.rawQuery)
        val body = x.requestBody.readBytes()
        requests.add("$method $path"); bodies.add("$method $path" to body.decodeToString())
        val auth = x.requestHeaders.getFirst("Authorization"); seenAuth.add(auth)
        if (auth != "Bearer $token") return fixtureError(x, "errors.invalid_token")
        failing.entries.firstOrNull { path.endsWith(it.key) }?.let { return reply(x, it.value) }
        if (method == "GET" && path == "/mobile/v1/capabilities") {
            val base = ContractFixtures["capabilities.capabilities"]
            val override = capabilityOverride
            val answer = if (override == null) base else FixtureCase("capabilities.override", JsonObject(mapOf(
                "request" to JsonObject(mapOf("method" to JsonPrimitive("GET"), "path" to JsonPrimitive(path))),
                "response" to JsonObject(mapOf("status" to JsonPrimitive(200), "body" to override)))))
            return respond(x, answer)
        }
        val key = "$method $path"
        val cases = scripted[key] ?: return fixtureError(x, "errors.not_found")
        val paged = cases.any { it.query.containsKey("cursor") }
        val case = if (paged) {
            // Pages are answered by their recorded cursor (the fixtures form one chain); a cursor no case names gets the
            // next scripted page, and one past the last case is unknown: 404.
            val exact = cases.indexOfFirst { it.query["cursor"] == query["cursor"] }
            val index = if (exact >= 0) exact else (lastPage[key] ?: -1) + 1
            if (index >= cases.size) return fixtureError(x, "errors.not_found")
            lastPage[key] = index
            cases[index]
        } else cases[minOf(consumed.merge(key, 1, Int::plus)!! - 1, cases.size - 1)]
        // The phone must send exactly what the contract says (content headers, JSON/text body, cursor).
        val problems = mutableListOf<String>()
        case.requestHeaders.filterKeys { it != "authorization" }.forEach { (k, v) ->
            if (x.requestHeaders.getFirst(k) != v) problems += "header $k: expected $v, got ${x.requestHeaders.getFirst(k)}"
        }
        case.requestBody?.let { expected ->
            val actual = runCatching { json.parseToJsonElement(body.decodeToString()) }.getOrNull()
            if (actual?.let(::withoutNulls) != withoutNulls(expected)) problems += "body: expected $expected, got ${body.decodeToString()}"
        }
        case.requestText?.let { expected ->
            val text = body.decodeToString()
            val same = if (expected.trimStart().startsWith("{")) runCatching { withoutNulls(json.parseToJsonElement(text)) == withoutNulls(json.parseToJsonElement(expected)) }.getOrDefault(false) else text == expected
            if (!same) problems += "body text differs"
        }
        if (problems.isNotEmpty()) { mismatches += "${case.key}: ${problems.joinToString("; ")}"; reply(x, FixtureCase("mismatch", JsonObject(mapOf(
            "request" to JsonObject(mapOf("method" to JsonPrimitive(method), "path" to JsonPrimitive(path))),
            "response" to JsonObject(mapOf("status" to JsonPrimitive(400), "body" to JsonObject(mapOf("code" to JsonPrimitive("fixture_request_mismatch"), "retryable" to JsonPrimitive(false))))))))); return }
        respond(x, case)
    }

    /** Optional fields the phone omits (e.g. Manifest `capture_host: null`) equal an explicit null in the fixture. */
    private fun withoutNulls(e: JsonElement): JsonElement = if (e is JsonObject) JsonObject(e.filterValues { it !is kotlinx.serialization.json.JsonNull }.mapValues { withoutNulls(it.value) }) else e

    private fun respond(x: HttpExchange, case: FixtureCase) {
        val drop = dropResponse.firstOrNull { x.requestURI.path.endsWith(it) }
        if (drop != null) { dropResponse.remove(drop); x.close() } else reply(x, case)
    }

    companion object {
        fun pairing(server: FakeMobileServer, pki: TestPki = server.pki, token: String = server.token) =
            Pairing(server.url, server.serverId, (ContractFixtures.pairingPayload()["device_id"] as JsonPrimitive).content, token, pki.caSha256, pki.caDer)

        /** The QR payload as the phone parses it: the fixture's fields with this test PKI's CA and this server's URL. */
        fun code(server: FakeMobileServer, pki: TestPki = server.pki, token: String = server.token, serverId: String = server.serverId,
                 caDer: ByteArray = pki.caDer, caSha: String = pki.caSha256): String {
            val p = ContractFixtures.pairingPayload()
            return JsonObject(p.toMutableMap().also {
                it["server_url"] = JsonPrimitive(server.url); it["server_id"] = JsonPrimitive(serverId); it["token"] = JsonPrimitive(token)
                it["ca_sha256"] = JsonPrimitive(caSha); it["ca_der_b64"] = JsonPrimitive(java.util.Base64.getEncoder().encodeToString(caDer))
            }).toString()
        }
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
