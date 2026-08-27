"""SQLite schema shared by ingestion, normalisation, and presentation."""

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS raw_responses (
    id INTEGER PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    cycle_started_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    url TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    responded_at TEXT,
    latency_ms REAL,
    http_version TEXT,
    status_code INTEGER,
    reason_phrase TEXT,
    headers_json TEXT,
    server_date TEXT,
    content_type TEXT,
    body_bytes INTEGER,
    body_text TEXT,
    body_b64 TEXT,
    body_is_json INTEGER,
    error_type TEXT,
    error_message TEXT
);
-- One provider's window, with the columns the drill-down reads riding along for the
-- same reason as the index below: the alternative is one random read of a table full
-- of captured bodies per attempt.
CREATE INDEX IF NOT EXISTS raw_responses_provider_window
    ON raw_responses(provider, requested_at, cycle_id, attempt, status_code, latency_ms);
CREATE INDEX IF NOT EXISTS raw_responses_cycle ON raw_responses(cycle_id);
-- The dashboard asks for a window across all providers, and the composite index
-- above cannot serve a range on its second column, so every page view scanned the
-- whole capture. Time alone is what that question is keyed by, and the columns it
-- reads ride along: a captured body is large, and going back to the table for one
-- turns a sequential read of a window into a random read of the whole capture.
CREATE INDEX IF NOT EXISTS raw_responses_window
    ON raw_responses(requested_at, provider, cycle_id, latency_ms, status_code);

CREATE TABLE IF NOT EXISTS processing_results (
    raw_response_id INTEGER PRIMARY KEY,
    processed_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    outcome TEXT NOT NULL,
    error_class TEXT,
    error_code TEXT,
    error_message TEXT,
    adapter_version TEXT NOT NULL,
    observations_written INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(raw_response_id) REFERENCES raw_responses(id)
);
CREATE INDEX IF NOT EXISTS processing_results_provider_time
    ON processing_results(provider, processed_at);
CREATE INDEX IF NOT EXISTS processing_results_outcome
    ON processing_results(outcome);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    raw_response_id INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    pay_model TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    value REAL NOT NULL,
    capacity REAL,
    unit TEXT NOT NULL,
    refresh_at TEXT,
    labels_json TEXT NOT NULL DEFAULT '{}',
    adapter_version TEXT NOT NULL,
    FOREIGN KEY(raw_response_id) REFERENCES raw_responses(id),
    UNIQUE(raw_response_id, metric_name, labels_json)
);
CREATE INDEX IF NOT EXISTS observations_provider_window
    ON observations(provider, observed_at, metric_name, value, unit, labels_json,
                    raw_response_id);
CREATE INDEX IF NOT EXISTS observations_metric_time
    ON observations(metric_name, observed_at);
CREATE INDEX IF NOT EXISTS observations_window
    ON observations(observed_at, provider, value, raw_response_id, labels_json);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    emitted_at TEXT NOT NULL,
    provider TEXT,
    rule TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'today',
    text TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    resolved_at TEXT
);
"""

# Indexes over columns that older databases gain by migration; they cannot run in
# the same script as the tables, because on such a database the column does not
# exist yet when the script executes.
POST_MIGRATION_SCHEMA = """
-- Superseded by the covering indexes above; kept as an explicit drop so a database
-- that already carries them does not keep paying for them on every insert.
DROP INDEX IF EXISTS raw_responses_time;
DROP INDEX IF EXISTS observations_time;
-- A strict prefix of raw_responses_provider_window, so it answers nothing extra.
DROP INDEX IF EXISTS raw_responses_provider_time;
-- A strict prefix of observations_provider_window.
DROP INDEX IF EXISTS observations_provider_time;
CREATE UNIQUE INDEX IF NOT EXISTS alerts_open_key
    ON alerts(dedupe_key) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS alerts_emitted ON alerts(emitted_at);
"""


# Columns added after the first deployments; existing databases are migrated in
# place because the captured responses must survive a schema change.
ADDED_COLUMNS = {
    "raw_responses": {"attempt": "INTEGER NOT NULL DEFAULT 1"},
    "alerts": {"severity": "TEXT NOT NULL DEFAULT 'today'", "resolved_at": "TEXT"},
}


ALERT_COLUMNS = (
    "emitted_at",
    "provider",
    "rule",
    "severity",
    "text",
    "evidence_json",
    "dedupe_key",
    "resolved_at",
)


def _drop_legacy_alert_uniqueness(connection: sqlite3.Connection) -> None:
    """Rebuild `alerts` when it still carries a table-wide UNIQUE on dedupe_key.

    The first schema made dedupe_key unique across the whole table, which silently
    means an alert can never reopen: once a condition has been raised and resolved,
    the same condition happening again would violate the constraint. Uniqueness
    belongs only to alerts that are still open, and SQLite cannot drop a column
    constraint, so the table is rebuilt once.
    """

    legacy = [
        row
        for row in connection.execute("PRAGMA index_list(alerts)")
        if row[1].startswith("sqlite_autoindex") and row[2]
    ]
    if not legacy:
        return
    columns = ", ".join(ALERT_COLUMNS)
    connection.executescript(
        f"""
        CREATE TABLE alerts_rebuilt (
            id INTEGER PRIMARY KEY,
            emitted_at TEXT NOT NULL,
            provider TEXT,
            rule TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'today',
            text TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            dedupe_key TEXT NOT NULL,
            resolved_at TEXT
        );
        INSERT INTO alerts_rebuilt ({columns}) SELECT {columns} FROM alerts;
        DROP TABLE alerts;
        ALTER TABLE alerts_rebuilt RENAME TO alerts;
        """
    )


def _migrate(connection: sqlite3.Connection) -> None:
    for table, columns in ADDED_COLUMNS.items():
        present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in present:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    _drop_legacy_alert_uniqueness(connection)


def initialise_database(path: Path) -> None:
    """Create the durable store without requiring an external service."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        _migrate(connection)
        connection.executescript(POST_MIGRATION_SCHEMA)
