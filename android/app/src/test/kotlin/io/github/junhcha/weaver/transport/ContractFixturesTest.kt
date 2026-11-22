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
import kotlinx.serialization.json.jsonPrimitive
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
        assertEquals(TransportFeature.entries.filter { it != TransportFeature.ALERT }.map { it.wire }.toSet(), (caps["transports"] as JsonObject).keys)
    }

    @Test fun alertFixturesMatchHandWrittenWireModels() {
        val caps = alertWireJson.decodeFromString<AlertCapabilities>(Fx.json("alerts.enabled_capabilities").toString())
        assertTrue(caps.http)
        listOf("list", "received_list", "other_device_list").forEach { name ->
            val envelope = WeaverCodec.json.decodeFromString<AlertEnvelope>(Fx.json("alerts.$name").toString())
            assertNull(envelope.nextCursor)
            envelope.items.forEach { CourierClient.decoded<Alert>(it.toString()) }
        }
        listOf("body", "body_after_receipt").forEach { CourierClient.decoded<Alert>(Fx.json("alerts.$it").toString()) }
        listOf("receipt", "receipt_retry").forEach {
            val ack = WeaverCodec.json.decodeFromString<AlertAck>(Fx.json("alerts.$it").toString())
            assertEquals("received", ack.state)
            assertEquals(CourierClient.decoded<Alert>(Fx.json("alerts.body").toString()).alertId, ack.alertId)
        }
        listOf("unknown", "unknown_ack").forEach {
            assertEquals(404, ContractFixtures["alerts.$it"].status)
            assertEquals(AlertError("alert_not_found", false), WeaverCodec.json.decodeFromString<AlertError>(Fx.json("alerts.$it").toString()))
        }
        listOf("expired_body", "expired_ack").forEach {
            assertEquals(410, ContractFixtures["alerts.$it"].status)
            val error = WeaverCodec.json.decodeFromString<AlertError>(Fx.json("alerts.$it").toString())
            assertEquals("alert_body_expired", error.code)
            assertFalse(error.retryable)
        }
    }

    @Test fun feedFixturesMatchTheHandWrittenEnvelopeAndNegotiateKinds() {
        fun feed(name: String) = WeaverCodec.json.decodeFromString<FeedEnvelope>(Fx.json(name).toString())
        listOf("changes.snapshot", "changes.incremental", "changes.up_to_date", "changes.tombstone").forEach { feed(it) }
        // An older build names no kinds and only ever receives the kinds it shipped with.
        assertTrue(feed("changes_kinds.legacy_snapshot").items.all { it.kind in LEGACY_FEED_KINDS })
        val all = feed("changes_kinds.negotiated_snapshot")
        assertEquals(setOf("alert", "fabric", "pipeline", "wiki"), all.items.map { it.kind }.toSet())
        assertTrue(all.items.none { it.removed })
        // Hints carry no Alert body; pipeline/wiki hints carry the ETag of the body to fetch.
        assertNull(all.items.first { it.kind == "alert" }.body)
        listOf("pipeline", "wiki").forEach { kind ->
            val body = all.items.first { it.kind == kind }.body!!.jsonObject
            assertTrue(body["etag"].toString().startsWith("\""))
        }
        assertEquals("tombstone", feed("changes_kinds.alert_body_expired").items.single().op)
        val offered = (Fx.json("pipeline.enabled_capabilities")["feed_kinds"] as JsonArray).map { it.jsonPrimitive.content }
        // Review runs over files in that scenario, so the review kind is not offered (and never tombstoned).
        assertEquals(listOf("fabric", "pipeline", "wiki"), offered)
        val onFiles = feed("changes_kinds.review_on_files")
        assertEquals(listOf("fabric"), onFiles.items.map { it.kind })
        assertTrue(onFiles.items.none { it.removed })
        // A cursor cut for one kind set is refused (410) for another: restart with a new snapshot.
        assertEquals(410, ContractFixtures["changes_kinds.kinds_changed_cursor"].status)
        assertEquals("cursor_expired", Fx.json("changes_kinds.kinds_changed_cursor")["code"]!!.jsonPrimitive.content)
        // The older APK path serves the same list as /read/pipeline.
        assertEquals(Fx.json("pipeline.list"), Fx.json("pipeline.older_apk_list"))
    }

    @Test fun pipelineFixturesDecodeAsPipelineItemsWithDefaultKeysOmitted() {
        val list = Fx.json("pipeline.list")["items"] as JsonArray
        assertEquals(1, list.size)
        val item = CourierClient.decoded<PipelineItem>(list[0].toString())
        assertEquals(1, item.revision)
        assertFalse(list[0].jsonObject.containsKey("capture_hosts"))
        assertFalse(list[0].jsonObject.containsKey("summary"))
        assertEquals(item, CourierClient.decoded<PipelineItem>(Fx.json("pipeline.item").toString()))
        assertEquals(304, ContractFixtures["pipeline.item_not_modified"].status)
        assertEquals("transport_files", Fx.json("pipeline.files_route")["code"]!!.jsonPrimitive.content)
        assertEquals("cursor_expired", Fx.json("pipeline.stale_cursor")["code"]!!.jsonPrimitive.content)
    }

    @Test fun alertEnvelopesAcksAndErrorsRejectUnknownKeys() {
        fun extra(name: String) = JsonObject(Fx.json(name) + ("unknown" to kotlinx.serialization.json.JsonPrimitive(true))).toString()
        assertThrows(kotlinx.serialization.SerializationException::class.java) {
            WeaverCodec.json.decodeFromString<AlertEnvelope>(extra("alerts.list"))
        }
        assertThrows(kotlinx.serialization.SerializationException::class.java) {
            WeaverCodec.json.decodeFromString<AlertAck>(extra("alerts.receipt"))
        }
        assertThrows(kotlinx.serialization.SerializationException::class.java) {
            WeaverCodec.json.decodeFromString<AlertError>(extra("alerts.unknown_ack"))
        }
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
