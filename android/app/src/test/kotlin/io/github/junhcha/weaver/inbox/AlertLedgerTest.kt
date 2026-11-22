package io.github.junhcha.weaver.inbox

import android.Manifest
import android.app.Application
import android.app.NotificationManager
import androidx.test.core.app.ApplicationProvider
import io.github.junhcha.weaver.courier.CourierClient
import io.github.junhcha.weaver.schema.Alert
import io.github.junhcha.weaver.transport.*
import java.io.File
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [26, 36])
class AlertLedgerTest {
    private lateinit var app: Application
    private val alert get() = CourierClient.decoded<Alert>(Fx.json("alerts.body").toString())
    private val server = "server-a"
    private val device = "device-a"

    @Before fun setup() {
        app = ApplicationProvider.getApplicationContext()
        File(app.filesDir, "inbox.sqlite3").delete()
        app.getSystemService(NotificationManager::class.java).cancelAll()
    }

    private fun reconcile(db: InboxDatabase, source: String, alerts: List<Alert> = listOf(alert), serverId: String? = null) =
        db.reconcile(emptyList(), alerts, emptySet(), 100, source, false, source, serverId)

    @Test fun rollbackKeepsUnreadBodyAndNeverRevivesClosedOrPostedIds() {
        val closed = alert.copy(alertId = "alt_01ARZ3NDEKTSV4RRFFQ69G5FA1")
        InboxDatabase(app).use { db ->
            db.bindAlertSource("saf:tree", server)
            reconcile(db, "http:$server", listOf(alert, closed), server)
            db.markAlertsNotified()
            db.markAlertRead(server, closed.alertId, 101)
            assertTrue(db.dismiss(closed.alertId, 102, server))
            db.alertAcknowledged(server, device, alert.alertId)
        }
        InboxDatabase(app).use { db ->
            assertEquals(listOf("alert_${closed.alertId}.json"), reconcile(db, "saf:tree", listOf(alert, closed)))
            val unread = db.alerts().single()
            assertEquals(alert.message, unread.message)
            assertTrue(unread.notified)
            // Empty snapshots and a disappearing adapter do not remove the unread body.
            repeat(3) { reconcile(db, "saf:tree", emptyList()) }
            assertEquals(alert.alertId, db.alerts().single().alertId)
            assertEquals(listOf("alert_${closed.alertId}.json"), reconcile(db, "saf:tree", listOf(alert, closed)))
            db.readableDatabase.rawQuery("SELECT read_at FROM alert_ledger WHERE server_id=? AND alert_id=?", arrayOf(server, alert.alertId)).use {
                assertTrue(it.moveToFirst()); assertTrue(it.isNull(0))
            }
        }
    }

    @Test fun fileHistoryBindsOnceAndMergesCloseAndPostIntoHttpHistory() {
        InboxDatabase(app).use { db ->
            reconcile(db, "saf:tree")
            db.markAlertsNotified()
            db.dismiss(alert.alertId, 101)
            db.receiveAlert(server, alert, 102)
            db.bindAlertSource("saf:tree", server)
            reconcile(db, "http:$server", serverId = server)
            assertTrue(db.alerts().isEmpty())
            assertEquals(listOf(alert.alertId), db.pendingAlertAcks(server, device))
            assertThrows(IllegalStateException::class.java) { db.bindAlertSource("saf:tree", "server-b") }
        }
    }

    @Test fun sameIdOnAnotherServerAndNewDeviceReceiptAreIsolated() {
        InboxDatabase(app).use { db ->
            reconcile(db, "http:$server", serverId = server)
            db.dismiss(alert.alertId, 101, server)
            db.alertAcknowledged(server, device, alert.alertId)
            reconcile(db, "http:server-b", listOf(alert.copy(message = "다른 서버")), "server-b")
            assertEquals("다른 서버", db.alerts().single().message)
            assertTrue(db.pendingAlertAcks(server, device).isEmpty())
            assertEquals(listOf(alert.alertId), db.pendingAlertAcks(server, "new-device"))
            reconcile(db, "http:$server", serverId = server)
            assertTrue(db.alerts().isEmpty())
        }
    }

    @Test fun conflictingBodyRollsBackReconcileAndDoesNotOverwriteHistory() {
        InboxDatabase(app).use { db ->
            reconcile(db, "tree")
            assertThrows(IllegalStateException::class.java) { reconcile(db, "tree", listOf(alert.copy(message = "conflict"))) }
            assertEquals(alert.message, db.alerts().single().message)
        }
    }

    @Test fun upgradeFromV3PreservesClosedPostedAndTemporarilyAbsentBodies() {
        InboxDatabase(app).use { it.writableDatabase }
        android.database.sqlite.SQLiteDatabase.openDatabase(File(app.filesDir, "inbox.sqlite3").path, null, 0).use { old ->
            listOf("alert_ledger", "alert_sources", "alert_receipts").forEach { old.execSQL("DROP TABLE $it") }
            old.execSQL("INSERT INTO settings VALUES(?, ?)", arrayOf("tree", "saf:tree"))
            old.execSQL("""INSERT INTO alerts(alert_id, package_id, thread_id, message, created_at, state, first_seen, changed_at, notified)
                VALUES(?, ?, ?, ?, ?, ?, 1, 1, 1)""", arrayOf(alert.alertId, alert.packageId, alert.threadId, alert.message, alert.createdAt, InboxDatabase.ABSENT))
            val closed = alert.copy(alertId = "alt_01ARZ3NDEKTSV4RRFFQ69G5FA1")
            old.execSQL("""INSERT INTO alerts(alert_id, package_id, thread_id, message, created_at, state, first_seen, changed_at, notified)
                VALUES(?, ?, ?, ?, ?, ?, 1, 1, 1)""", arrayOf(closed.alertId, closed.packageId, closed.threadId, closed.message, closed.createdAt, InboxDatabase.DISMISSED))
            old.version = 3
        }
        InboxDatabase(app).use { db ->
            assertEquals(alert.alertId, db.alerts().single().alertId)
            assertTrue(db.alerts().single().notified)
            db.bindAlertSource("saf:tree", server)
            reconcile(db, "http:$server", serverId = server)
            assertEquals(alert.message, db.alerts().single().message)
            assertEquals(2, db.pendingAlertAcks(server, device).size)
        }
    }

    @Test fun crashBeforeNotificationPostRecoversSilentlyAndAfterPostDoesNotRepost() {
        shadowOf(app).grantPermissions(Manifest.permission.POST_NOTIFICATIONS)
        val signature = "$server:${alert.alertId}"
        InboxDatabase(app).use { db ->
            reconcile(db, "http:$server", serverId = server)
            db.prepareAlertsNotification(signature) // crash before Android notify
        }
        val manager = app.getSystemService(NotificationManager::class.java)
        InboxDatabase(app).use { InboxRuntime.notifyAlerts(app, it) }
        val notification = manager.activeNotifications.single().notification
        assertEquals(android.app.Notification.GROUP_ALERT_SUMMARY, notification.groupAlertBehavior)
        InboxDatabase(app).use { it.prepareAlertsNotification(signature) } // crash after notify before pending clear
        InboxDatabase(app).use { InboxRuntime.notifyAlerts(app, it) }
        assertSame(notification, manager.activeNotifications.single().notification)
        InboxDatabase(app).use { assertEquals("", it.setting("alert_pending")) }
    }

    @Test fun httpAckFollowsReceiptCommitSurvivesAdapterLossAndIgnoresReviewRoute() {
        FakeMobileServer().use { remote ->
            remote.serve("alerts.list", "alerts.receipt")
            val spool = SpoolFolder(File(app.cacheDir, "alert-spool"))
            val state = TransportState(File(app.cacheDir, "alert-state.json"))
            val ledger = TransportRuntime.inboxAlertLedger(app, server, device)
            fun exchange() = HttpExchange(HttpsMobileApi(FakeMobileServer.pairing(remote)), spool, state,
                { if (it == TransportFeature.ALERT) TransportRoute.HTTP else TransportRoute.FILES }, { true }, ledger)
            exchange().pull()
            InboxDatabase(app).use { assertEquals(listOf(alert), it.alertBodies(server)) }
            assertEquals(0, remote.count("/ack"))
            spool.delete("proposals", "alert_${alert.alertId}.json")
            // A new exchange after process death finds receipt work in SQLite, not the adapter/state.json.
            exchange().push()
            assertEquals(1, remote.count("/ack"))
            assertTrue(ledger.pending().isEmpty())
            assertTrue(remote.requests.none { "/reviews" in it })
            remote.reset("alerts.list"); remote.serve("alerts.received_list")
            exchange().pull()
            InboxDatabase(app).use { assertEquals(listOf(alert), it.alertBodies(server)) }
        }
    }

    @Test fun failedReceiptCommitCannotAckAndCanRetry() {
        FakeMobileServer().use { remote ->
            remote.serve("alerts.list", "alerts.receipt")
            val real = TransportRuntime.inboxAlertLedger(app, server, device)
            val ledger = object : AlertLedger by real {
                override fun receive(alert: Alert) { throw android.database.sqlite.SQLiteException("synthetic disk failure") }
            }
            val exchange = HttpExchange(HttpsMobileApi(FakeMobileServer.pairing(remote)), SpoolFolder(File(app.cacheDir, "fail-spool")),
                TransportState(File(app.cacheDir, "fail-state.json")), { TransportRoute.HTTP }, { true }, ledger)
            exchange.pull(); exchange.push()
            assertTrue(exchange.summary.retry)
            assertEquals(0, remote.count("/ack"))
            assertTrue(real.pending().isEmpty())
        }
    }

    @Test fun crashAfterServerAckBeforeLocalCompletionReplaysTheSameAck() {
        FakeMobileServer().use { remote ->
            remote.serve("alerts.receipt", "alerts.receipt_retry")
            val real = TransportRuntime.inboxAlertLedger(app, server, device)
            real.receive(alert)
            val crash = object : AlertLedger by real {
                override fun acknowledged(alertId: String) { throw android.database.sqlite.SQLiteException("simulated crash") }
            }
            val spool = SpoolFolder(File(app.cacheDir, "ack-crash-spool"))
            val state = TransportState(File(app.cacheDir, "ack-crash-state.json"))
            fun exchange(ledger: AlertLedger) = HttpExchange(HttpsMobileApi(FakeMobileServer.pairing(remote)), spool, state,
                { if (it == TransportFeature.ALERT) TransportRoute.HTTP else TransportRoute.FILES }, { true }, ledger)
            assertTrue(exchange(crash).push().retry)
            assertEquals(listOf(alert.alertId), real.pending())
            exchange(real).push()
            assertEquals(2, remote.count("/ack"))
            assertTrue(real.pending().isEmpty())
        }
    }

    @Test fun explicitVersionedAlertRouteIsIndependentAndNeverDualActive() {
        val box = object : SecretBox {
            override fun seal(plain: ByteArray) = plain
            override fun open(sealed: ByteArray) = sealed
        }
        val settings = TransportSettings(app.getSharedPreferences("alert-route", 0), box)
        settings.pair(Pairing("https://synthetic", server, device, "synthetic-token", "0".repeat(64), byteArrayOf(1)))
        val old = Fx.json("capabilities.capabilities")
        val reviewOnly = kotlinx.serialization.json.JsonObject(old + ("transports" to kotlinx.serialization.json.JsonObject(
            mapOf("review" to kotlinx.serialization.json.JsonPrimitive("http")))))
        settings.applyCapabilities(reviewOnly)
        assertEquals(TransportRoute.FILES, settings.route(TransportFeature.ALERT))
        val enabled = Fx.json("alerts.enabled_capabilities")
        settings.applyCapabilities(kotlinx.serialization.json.JsonObject(enabled - "schema_versions"))
        assertEquals(TransportRoute.FILES, settings.route(TransportFeature.ALERT))
        settings.applyCapabilities(enabled)
        assertEquals(TransportRoute.HTTP, settings.route(TransportFeature.ALERT))
        assertEquals(TransportRoute.FILES, settings.route(TransportFeature.VERDICT))
        val files = io.github.junhcha.weaver.courier.MemoryFolder()
        val spool = SpoolFolder(File(app.cacheDir, "routes-spool"))
        val name = "alert_${alert.alertId}.json"
        files.put("proposals", name, io.github.junhcha.weaver.schema.WeaverCodec.encode(alert.copy(message = "files")))
        spool.put("proposals", name, io.github.junhcha.weaver.schema.WeaverCodec.encode(alert).toByteArray())
        val routed = RoutedFolder(files, spool, settings::route, settings::feature)
        assertEquals(1, routed.list("proposals").size)
        assertEquals(alert.message, CourierClient.decoded<Alert>(routed.read("proposals", name).reader().use { it.readText() }).message)
        settings.applyCapabilities(old)
        assertEquals(1, routed.list("proposals").size)
        assertEquals("files", CourierClient.decoded<Alert>(routed.read("proposals", name).reader().use { it.readText() }).message)
    }

    @Test fun expiredBodyAndAckNeverDeleteThePhonesBody() {
        FakeMobileServer().use { remote ->
            remote.serve("alerts.expired_body", "alerts.expired_ack")
            val api = HttpsMobileApi(FakeMobileServer.pairing(remote))
            val ledger = TransportRuntime.inboxAlertLedger(app, server, device)
            ledger.receive(alert)
            val error = assertThrows(ApiException::class.java) { api.alert(alert.alertId) }
            assertEquals(410, error.status)
            assertEquals("alert_body_expired", error.code)
            val exchange = HttpExchange(api, SpoolFolder(File(app.cacheDir, "expired-spool")),
                TransportState(File(app.cacheDir, "expired-state.json")),
                { if (it == TransportFeature.ALERT) TransportRoute.HTTP else TransportRoute.FILES }, { true }, ledger)
            exchange.push()
            assertFalse(exchange.summary.retry)
            assertTrue(ledger.pending().isEmpty())
            InboxDatabase(app).use { assertEquals(listOf(alert), it.alertBodies(server)) }
        }
    }
}
