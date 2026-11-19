package io.github.junhcha.weaver.transport

import io.github.junhcha.weaver.courier.CourierClient
import io.github.junhcha.weaver.phone.HttpPhoneReadModel
import io.github.junhcha.weaver.schema.*
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.*
import org.junit.Test

/** Every shared fixture parses with the phone's own models: the contract and the phone cannot drift silently. */
class ContractFixturesTest {
    @Test fun domainBodiesDecodeWithTheStrictPhoneModels() {
        CourierClient.decoded<Manifest>(Fx.manifestText)
        CourierClient.decoded<Receipt>(Fx.json("captures.receipt").toString())
        CourierClient.decoded<Receipt>(Fx.json("captures.manifest_stored").toString())
        CourierClient.decoded<ReviewPackage>(Fx.json("reviews.get_revision").toString())
        CourierClient.decoded<Action>(Fx.actionText)
        CourierClient.decoded<ActionResult>(Fx.resultText)
        Fx.verdict; Fx.conflictingVerdict; Fx.staleVerdict
    }

    @Test fun capabilitiesCarryTheTransportsAndFeaturesThePhoneFollows() {
        val caps = Fx.json("capabilities.capabilities")
        assertEquals(setOf("capture", "review", "action"), (caps["transports"] as JsonObject).keys)
        assertEquals(false, (caps["features"] as JsonObject)["alerts"].toString().toBoolean())
        assertEquals(TransportFeature.entries.map { it.wire }.toSet(), (caps["transports"] as JsonObject).keys)
    }

    @Test fun everyReadFixtureThreadParsesAndKeepsTheServersOwnFlags() {
        val items = Fx.json("read.threads_first_page")["items"] as JsonArray
        val root = buildJsonObject {
            put("threads", JsonArray(items + (Fx.json("read.threads_next_page")["items"] as JsonArray) + (Fx.json("read.threads_last_page")["items"] as JsonArray)))
            put("tags", Fx.json("read.tags")["items"]!!)
            put("wiki", Fx.json("wiki.list")["items"]!!)
        }
        val unknown = AtomicInteger(); val invalid = AtomicInteger()
        val snapshot = HttpPhoneReadModel.parse(root, unknown, invalid)
        assertEquals(0, unknown.get() + invalid.get())
        assertTrue(snapshot.threads.all { it.open != null && it.retracted != null && it.mentions != null && it.presentation != null })
        val t1 = snapshot.threads.first { it.id == "t1" }
        assertEquals(java.time.LocalDate.parse("2026-11-10"), t1.deadline)
        assertEquals(listOf("협력사 A"), t1.mentions)
        assertTrue(t1.open == true)
        val schedule = snapshot.threads.first { it.id == "t4" }
        assertEquals(java.time.Instant.parse("2026-11-12T06:00:00Z"), schedule.scheduleAt)
        assertEquals(2, snapshot.wikiPages.size)
        assertEquals("방화벽 교체", snapshot.wikiPages.last().title)
        assertTrue("list entries carry no body", snapshot.wikiPages.all { it.body.isEmpty() && it.sha256 != null })
    }

    @Test fun everyErrorFixtureHasTheFixedShape() {
        ContractFixtures.cases.filterKeys { it.startsWith("errors.") }.values.forEach { case ->
            val body = case.responseBody!!.jsonObject
            assertTrue(case.key, body.containsKey("code") && body.containsKey("retryable"))
        }
    }
}
