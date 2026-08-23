"""Classify and normalise stored raw responses without losing source evidence."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from explee_test.observability.adapters import (
    ADAPTER_VERSION,
    PROVIDERS,
    AdapterError,
    SemanticError,
    normalize_payload,
)
from explee_test.observability.store import initialise_database


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _classify(row: sqlite3.Row) -> tuple[str, str | None, str | None, str | None, Any]:
    if row["error_type"]:
        return (
            "transport_error",
            "transport",
            row["error_type"],
            row["error_message"],
            None,
        )
    if row["status_code"] == 429:
        # Throttling is an instruction with a stated delay, not a broken provider.
        return "throttled", "throttle", "http_429", row["reason_phrase"], None
    if row["status_code"] != 200:
        status = row["status_code"]
        return "http_error", "http", f"http_{status}", row["reason_phrase"], None
    if row["body_is_json"] != 1:
        return "invalid_json", "payload", "invalid_json", "response body is not JSON", None
    try:
        payload = json.loads(row["body_text"])
    except (TypeError, json.JSONDecodeError) as exc:
        return "invalid_json", "payload", "invalid_json", str(exc), None
    if payload == {}:
        return "empty_payload", "payload", "empty_object", "response body is {}", None
    return "candidate", None, None, None, payload


def _pending(connection: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT r.*
        FROM raw_responses AS r
        LEFT JOIN processing_results AS p ON p.raw_response_id = r.id
        WHERE p.raw_response_id IS NULL AND r.provider != '_catalog'
        ORDER BY r.id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def process_pending(path: Path, limit: int = 2_000) -> int:
    """Process at most ``limit`` raw rows and return the number classified."""

    initialise_database(path)
    with sqlite3.connect(path, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = _pending(connection, limit)
        for row in rows:
            outcome, error_class, error_code, error_message, payload = _classify(row)
            samples = []
            if outcome == "candidate":
                try:
                    samples = normalize_payload(row["provider"], payload)
                    outcome = "success"
                except SemanticError as exc:
                    outcome, error_class, error_code, error_message = (
                        "semantic_error",
                        "semantic",
                        "invalid_value",
                        str(exc),
                    )
                except AdapterError as exc:
                    outcome, error_class, error_code, error_message = (
                        "schema_error",
                        "schema",
                        "schema_mismatch",
                        str(exc),
                    )

            definition = PROVIDERS.get(row["provider"])
            for sample in samples:
                connection.execute(
                    """
                    INSERT INTO observations (
                        raw_response_id, observed_at, provider, pay_model,
                        metric_name, value, capacity, unit, refresh_at,
                        labels_json, adapter_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        row["requested_at"],
                        row["provider"],
                        definition.pay_model if definition else "unknown",
                        sample.metric_name,
                        sample.value,
                        sample.capacity,
                        sample.unit,
                        sample.refresh_at,
                        json.dumps(sample.labels, sort_keys=True, separators=(",", ":")),
                        ADAPTER_VERSION,
                    ),
                )
            connection.execute(
                """
                INSERT INTO processing_results (
                    raw_response_id, processed_at, provider, outcome, error_class,
                    error_code, error_message, adapter_version, observations_written
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    _utc_now(),
                    row["provider"],
                    outcome,
                    error_class,
                    error_code,
                    (error_message or "")[:2_000] or None,
                    ADAPTER_VERSION,
                    len(samples),
                ),
            )
        return len(rows)


def process_all_pending(path: Path, batch_size: int = 2_000) -> int:
    """Backfill all unprocessed rows in bounded transactions."""

    total = 0
    while processed := process_pending(path, batch_size):
        total += processed
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/raw.sqlite3"))
    parser.add_argument("--batch-size", type=int, default=2_000)
    args = parser.parse_args()
    print(f"processed {process_all_pending(args.db, args.batch_size)} raw responses")


if __name__ == "__main__":
    main()
