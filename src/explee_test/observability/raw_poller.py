"""Raw-telemetry poller.

Captures every provider response verbatim — status, headers, body, server time,
latency, transport errors — without interpreting it. Interpretation happens later,
from the stored rows. Nothing a provider returns (or fails to return) may stop the loop.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import signal
import sqlite3
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from explee_test.observability.alerts import evaluate_and_store
from explee_test.observability.normalizer import process_pending
from explee_test.observability.store import initialise_database
from explee_test.settings import get_settings

log = logging.getLogger("raw_poller")

CATALOG_PSEUDO_PROVIDER = "_catalog"

# A 429 carrying Retry-After is an instruction, not a failure: the provider states
# when the same request is welcome again. One bounded retry honours it without
# turning the loop into a queue; anything longer is left for the next cycle. The
# same single retry recovers the other two transient shapes this API produces: an
# empty object at HTTP 200, and an isolated 5xx.
MAX_RETRY_WAIT_SECONDS = 10.0
RETRY_ATTEMPTS = 1
EMPTY_BODY_DELAY_SECONDS = 1.0
SERVER_ERROR_DELAY_SECONDS = 2.0

# Fallback catalog, used only if the live catalog endpoint cannot be read this cycle.
FALLBACK_ENDPOINTS: dict[str, str] = {
    p: f"/api/{p}/balance"
    for p in [
        "brightdata",
        "evomi",
        "scrapfly",
        "twocaptcha",
        "zerobounce",
        "findymail",
        "bounceban",
        "openai",
        "openrouter",
        "anthropic",
        "elevenlabs",
        "tremendous",
        "vastai",
        "meta_ads",
        "resend",
    ]
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _site_root(api_base: str) -> str:
    """Catalog endpoints are given as '/api/<p>/balance' relative to the site root."""
    return api_base.removesuffix("/").removesuffix("/api")


async def fetch_one(
    client: httpx.AsyncClient,
    provider: str,
    url: str,
    cycle_id: str,
    cycle_started_at: str,
    attempt: int = 1,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "cycle_id": cycle_id,
        "attempt": attempt,
        "cycle_started_at": cycle_started_at,
        "provider": provider,
        "url": url,
        "requested_at": utc_now(),
        "responded_at": None,
        "latency_ms": None,
        "http_version": None,
        "status_code": None,
        "reason_phrase": None,
        "headers_json": None,
        "server_date": None,
        "content_type": None,
        "body_bytes": None,
        "body_text": None,
        "body_b64": None,
        "body_is_json": None,
        "error_type": None,
        "error_message": None,
    }
    t0 = time.perf_counter()
    try:
        resp = await client.get(url)
        row["latency_ms"] = round((time.perf_counter() - t0) * 1000, 3)
        row["responded_at"] = utc_now()
        row["http_version"] = resp.http_version
        row["status_code"] = resp.status_code
        row["reason_phrase"] = resp.reason_phrase
        row["headers_json"] = json.dumps(resp.headers.multi_items(), ensure_ascii=False)
        row["server_date"] = resp.headers.get("date")
        row["content_type"] = resp.headers.get("content-type")
        body = resp.content
        row["body_bytes"] = len(body)
        try:
            row["body_text"] = body.decode("utf-8")
        except UnicodeDecodeError:
            row["body_text"] = body.decode("utf-8", errors="replace")
            row["body_b64"] = base64.b64encode(body).decode("ascii")
        try:
            json.loads(row["body_text"])
            row["body_is_json"] = 1
        except (ValueError, TypeError):
            row["body_is_json"] = 0
    except Exception as exc:  # noqa: BLE001 - every failure is data, never fatal
        row["latency_ms"] = round((time.perf_counter() - t0) * 1000, 3)
        row["responded_at"] = utc_now()
        row["error_type"] = type(exc).__name__
        row["error_message"] = str(exc)[:2000]
    return row


def retry_after_header(row: dict[str, Any]) -> str | None:
    """The raw Retry-After value, or None when the provider did not state one."""

    if not row.get("headers_json"):
        return None
    try:
        headers = json.loads(row["headers_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    for name, value in headers:
        if name.lower() == "retry-after":
            return value
    return None


def retry_delay(row: dict[str, Any]) -> float | None:
    """How long to wait before one retry, or None to leave it for the next cycle.

    A stated delay always wins, including when it is too long for this cycle (504
    asks for two minutes, and honouring that means not asking again now) and when
    it is stated in a form this loop does not parse. Substituting our own guess for
    an instruction we failed to read would be worse than waiting for the next poll.
    Where nothing is stated the delay is short, because these failures are
    single-sample glitches rather than congestion.
    """

    stated = retry_after_header(row)
    if stated is not None:
        try:
            requested = float(stated)
        except (TypeError, ValueError):
            return None  # HTTP-date form: not worth parsing for a 30s loop
        return requested if 0 <= requested <= MAX_RETRY_WAIT_SECONDS else None
    status = row.get("status_code")
    if status is None:
        return None  # transport failures are not retried inside the cycle
    if status >= 500:
        return SERVER_ERROR_DELAY_SECONDS
    if status == 200 and (row.get("body_text") or "").strip() == "{}":
        return EMPTY_BODY_DELAY_SECONDS
    return None


async def retry_recoverable(
    client: httpx.AsyncClient,
    rows: list[dict[str, Any]],
    endpoints: dict[str, str],
    root: str,
    cycle_id: str,
    cycle_started_at: str,
    failing: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Re-request what a single retry can recover, once, after the stated delay.

    Retries are additional rows, never replacements: the first attempt stays in the
    record with its own outcome, so collection quality remains auditable.

    A provider that already failed the previous cycle is not retried. Retrying a
    blip is useful; doubling the request rate against something that is genuinely
    down only adds load to a service that is already struggling.
    """

    failing = failing or set()
    waits = {
        row["provider"]: seconds
        for row in rows
        if (seconds := retry_delay(row)) is not None
        and row["provider"] in endpoints
        and row["provider"] not in failing
    }
    if not waits:
        return []
    await asyncio.sleep(max(waits.values()))
    return list(
        await asyncio.gather(
            *(
                fetch_one(
                    client,
                    provider,
                    f"{root}{endpoints[provider]}",
                    cycle_id,
                    cycle_started_at,
                    attempt=RETRY_ATTEMPTS + 1,
                )
                for provider in waits
            )
        )
    )


def _extract_endpoints(catalog_row: dict[str, Any]) -> dict[str, str] | None:
    if catalog_row.get("status_code") != 200 or not catalog_row.get("body_is_json"):
        return None
    try:
        items = json.loads(catalog_row["body_text"])
        out = {}
        for item in items:
            p, ep = item.get("provider"), item.get("endpoint")
            if isinstance(p, str) and isinstance(ep, str):
                out[p] = ep
        return out or None
    except Exception:  # noqa: BLE001
        return None


def failed_providers(rows: list[dict[str, Any]]) -> set[str]:
    """Providers that produced no usable response in this cycle, retries included."""

    served: set[str] = set()
    attempted: set[str] = set()
    for row in rows:
        attempted.add(row["provider"])
        if row["status_code"] == 200 and (row.get("body_text") or "").strip() not in {"", "{}"}:
            served.add(row["provider"])
    return attempted - served


class Sink:
    """Write rows to SQLite and JSONL. Either may fail; the other still gets the row."""

    def __init__(self, db_path: Path, jsonl_path: Path) -> None:
        self.db_path = db_path
        self.jsonl_path = jsonl_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        initialise_database(db_path)

    def write(self, rows: list[dict[str, Any]]) -> None:
        try:
            with open(self.jsonl_path, "a", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            log.exception("jsonl write failed")
        try:
            cols = list(rows[0].keys())
            sql = (
                f"INSERT INTO raw_responses ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
            )
            with sqlite3.connect(self.db_path, timeout=30) as c:
                c.executemany(sql, [tuple(r[k] for k in cols) for r in rows])
        except Exception:  # noqa: BLE001
            log.exception("sqlite write failed")


async def run_cycle(
    client: httpx.AsyncClient,
    api_base: str,
    sink: Sink,
    failing: set[str] | None = None,
) -> list[dict[str, Any]]:
    cycle_id = uuid.uuid4().hex[:12]
    started = utc_now()
    root = _site_root(api_base)
    catalog_row = await fetch_one(
        client, CATALOG_PSEUDO_PROVIDER, f"{api_base}/providers", cycle_id, started
    )
    endpoints = _extract_endpoints(catalog_row) or FALLBACK_ENDPOINTS
    rows = await asyncio.gather(
        *(fetch_one(client, p, f"{root}{ep}", cycle_id, started) for p, ep in endpoints.items())
    )
    all_rows = [catalog_row, *rows]
    all_rows.extend(
        await retry_recoverable(client, all_rows, endpoints, root, cycle_id, started, failing)
    )
    sink.write(all_rows)
    process_pending(sink.db_path)
    try:
        verdict = evaluate_and_store(sink.db_path, get_settings().alerts_path)
    except Exception:  # noqa: BLE001 - policy must never stop collection
        log.exception("alert evaluation failed")
        verdict = {}
    errs = sum(1 for r in all_rows if r["error_type"] or r["status_code"] != 200)
    non_json = sum(1 for r in all_rows if r["body_is_json"] == 0)
    retried = sum(1 for r in all_rows if r["attempt"] > 1)
    log.info(
        "cycle %s: %d rows (%d retried), %d non-200/error, %d non-json, catalog=%s, alerts=%s",
        cycle_id,
        len(all_rows),
        retried,
        errs,
        non_json,
        "live" if endpoints is not FALLBACK_ENDPOINTS else "FALLBACK",
        "{open} open (+{opened}/-{resolved})".format(
            open=verdict.get("open", 0),
            opened=verdict.get("opened", 0),
            resolved=verdict.get("resolved", 0),
        ),
    )
    return all_rows


async def main_loop(interval: float, db_path: Path, jsonl_path: Path) -> None:
    settings = get_settings()
    sink = Sink(db_path, jsonl_path)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=0)
    async with httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        limits=limits,
        headers={"User-Agent": "explee-raw-poller/0.1"},
        follow_redirects=False,
    ) as client:
        log.info("poller started, interval=%ss, db=%s", interval, db_path)
        failing: set[str] = set()
        while not stop.is_set():
            t0 = time.monotonic()
            try:
                rows = await run_cycle(client, settings.api_base, sink, failing)
                failing = failed_providers(rows)
            except Exception:  # noqa: BLE001
                log.exception("cycle failed unexpectedly")
            delay = max(0.0, interval - (time.monotonic() - t0))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
    log.info("poller stopped")


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--db", type=Path, default=Path("data/raw.sqlite3"))
    ap.add_argument("--jsonl", type=Path, default=Path("data/raw_responses.jsonl"))
    ns = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(main_loop(ns.interval, ns.db, ns.jsonl))


if __name__ == "__main__":
    main()
