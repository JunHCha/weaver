-- Actual InboxDatabase v3 schema from origin/main 4708da8.
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE reviews (
            package_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, summary TEXT NOT NULL,
            stage TEXT NOT NULL, revised INTEGER NOT NULL, state TEXT NOT NULL,
            first_seen INTEGER NOT NULL, changed_at INTEGER NOT NULL,
            notified_revision INTEGER NOT NULL DEFAULT 0, absent_scans INTEGER NOT NULL DEFAULT 0);
CREATE TABLE submitted (package_id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
CREATE TABLE alerts (
            alert_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, thread_id TEXT NOT NULL,
            message TEXT NOT NULL, created_at TEXT NOT NULL, state TEXT NOT NULL,
            delete_pending INTEGER NOT NULL DEFAULT 0, first_seen INTEGER NOT NULL,
            changed_at INTEGER NOT NULL, notified INTEGER NOT NULL DEFAULT 0);


-- Actual first PR #203 APK schema (11929a7).
CREATE TABLE alert_ledger (
            server_id TEXT NOT NULL, alert_id TEXT NOT NULL, body TEXT NOT NULL,
            received_at INTEGER NOT NULL, posted INTEGER NOT NULL DEFAULT 0,
            read_at INTEGER, closed_at INTEGER, PRIMARY KEY(server_id, alert_id));
CREATE TABLE alert_sources (source TEXT PRIMARY KEY, server_id TEXT NOT NULL);
CREATE TABLE alert_receipts (server_id TEXT NOT NULL, device_id TEXT NOT NULL,
            alert_id TEXT NOT NULL, PRIMARY KEY(server_id, device_id, alert_id));
PRAGMA user_version=4;
