"""Alert policy: which observations deserve a human, and what they must carry.

Rules run over the same projection the dashboard draws, so every alert is
explainable from what a reader can see, and every threshold is reproducible from
stored responses. Detection lives in `events.py`; this module only decides.

Alerts are states, not messages. A rule declares which alerts should be open right
now, and the store is reconciled against that: a condition that became true opens
an alert once, a condition that stopped being true resolves it once. Nothing
repeats every thirty seconds while a provider is quietly running out.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from explee_test.observability.dashboard import _parse_time, _rate_per_hour, build_overview
from explee_test.observability.store import initialise_database

# Severity is a statement about what the reader should do, not a colour.
PAGE = "page"
TODAY = "today"
FYI = "fyi"

# Runs of cycles without data are bimodal in this feed: 556 runs of one or two
# cycles, one run of five, and 39 runs of eleven or more. The threshold sits in
# that empty band, so a blip never reaches it and an incident always does.
SILENT_CYCLES = 3
SILENT_CYCLES_URGENT = 10
COLLECTOR_SILENCE_SECONDS = 180
MIN_PROVIDERS_FOR_API_DOWN = 3

RUNWAY_THRESHOLDS = ((1.0, PAGE), (5.0, TODAY), (24.0, FYI))
# A prediction wobbles with its estimate; an alert that opens at 24h must not
# close at 24.1h and open again a minute later.
RUNWAY_HYSTERESIS = 1.5
# A rate needs samples before it means anything, and a restart has none.
WARMUP_CYCLES = 20

SPEND_ANOMALY_FACTOR = 3.0
SPEND_RECENT_MINUTES = 20
SPEND_BASELINE_HOURS = 4
SPEND_MIN_BASELINE_POINTS = 60

# Two processes write alerts, and each may only resolve what it is able to judge.
# The collector cannot report its own death, and the watchdog knows nothing about
# provider balances, so neither may close the other's alerts.
COLLECTOR_RULES = frozenset({"collector_dead"})
PROVIDER_RULES = frozenset(
    {
        "api_down",
        "provider_silent",
        "exhausted",
        "runway_1h",
        "runway_5h",
        "runway_24h",
        "spend_anomaly",
        "schema_drift",
    }
)


@dataclass(frozen=True)
class Alert:
    """One condition that is true now, with the evidence that makes it checkable."""

    key: str
    rule: str
    severity: str
    text: str
    provider: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


def _fmt(value: float | None, unit: str) -> str:
    if value is None:
        return "unknown"
    sign = "-" if value < 0 else ""
    if unit == "usd":
        return f"{sign}${abs(value):,.2f}"
    if unit == "gbp":
        return f"{sign}£{abs(value):,.2f}"
    return f"{value:,.0f} {unit}"


def _hours(value: float) -> str:
    if value < 1:
        return f"{value * 60:.0f} min"
    return f"{value:.1f} h" if value < 48 else f"{value / 24:.1f} d"


def evaluate(
    overview: dict[str, Any], open_keys: set[str], path: Path | None = None
) -> list[Alert]:
    """Return every alert that should be open for this projection."""

    alerts: list[Alert] = []
    providers = overview["providers"]
    silent = [item for item in providers if (item.get("missing_cycles") or 0) >= SILENT_CYCLES]

    # One incident, not fifteen: when everything stops at once, the API is down.
    # Below a handful of providers "all of them" says less than naming each.
    if len(providers) >= MIN_PROVIDERS_FOR_API_DOWN and len(silent) == len(providers):
        worst = max(item["missing_cycles"] for item in silent)
        return [
            Alert(
                key="api_down",
                rule="api_down",
                severity=PAGE,
                text=f"No data from any provider for {worst} polling cycles",
                evidence={"providers": len(providers), "missing_cycles": worst},
            )
        ]

    for item in silent:
        missing = item["missing_cycles"]
        alerts.append(
            Alert(
                key=f"silent:{item['provider']}",
                rule="provider_silent",
                severity=TODAY if missing >= SILENT_CYCLES_URGENT else FYI,
                text=(
                    f"{item['name']} returned no usable data for {missing} polling cycles"
                ),
                provider=item["provider"],
                evidence={
                    "missing_cycles": missing,
                    "last_attempt": (item.get("last_attempt") or {}).get("outcome"),
                    "last_valid_at": item.get("observed_at"),
                },
            )
        )

    silent_names = {item["provider"] for item in silent}
    for item in providers:
        provider = item["provider"]
        if provider in silent_names:
            continue  # unknown is not zero: never predict through a blind spot
        alerts.extend(_money_alerts(item, open_keys))
        if path is not None:
            alerts.extend(_spend_anomaly(path, item))
        if (item.get("outcomes") or {}).get("normalization"):
            alerts.append(
                Alert(
                    key=f"schema_drift:{provider}",
                    rule="schema_drift",
                    severity=TODAY,
                    text=(
                        f"{item['name']} returned a payload the adapter cannot read; "
                        "the provider contract or our parser has changed"
                    ),
                    provider=provider,
                    evidence={"failed_attempts": item["outcomes"]["normalization"]},
                )
            )
    return alerts


def _money_alerts(item: dict[str, Any], open_keys: set[str]) -> list[Alert]:
    value = item.get("value")
    forecast = item.get("forecast") or {}
    if value is None:
        return []

    if value <= 0:
        # Opens once when the balance crosses zero downward and stays open while it
        # is below; it never re-fires for a provider that lives in debt.
        return [
            Alert(
                key=f"exhausted:{item['provider']}",
                rule="exhausted",
                severity=PAGE,
                text=(
                    f"{item['name']} has crossed zero: {_fmt(value, item['unit'])}"
                    + (" (postpaid, now accruing debt)" if item["pay_model"] == "postpaid" else "")
                ),
                provider=item["provider"],
                evidence={"value": value, "unit": item["unit"], "observed_at": item["observed_at"]},
            )
        ]

    hours = item.get("risk_hours")
    if hours is None or (item.get("cycles") or 0) < WARMUP_CYCLES:
        return []
    # Only the most urgent crossed threshold is raised. A provider with 1.4 hours
    # left does not need three alerts saying so; as the runway shrinks the lower
    # alert resolves and the more urgent one opens, which reads as an escalation.
    for threshold, severity in RUNWAY_THRESHOLDS:
        key = f"runway_{threshold:g}h:{item['provider']}"
        limit = threshold * RUNWAY_HYSTERESIS if key in open_keys else threshold
        if hours > limit:
            continue
        note = ""
        if forecast.get("kind") == "before_refresh":
            note = f", and its package refreshes only in {_hours(forecast['refresh_hours'])}"
        return [
            Alert(
                key=key,
                rule=f"runway_{threshold:g}h",
                severity=severity,
                text=(
                    f"{item['name']} runs out in {_hours(hours)} at "
                    f"{_fmt(item['rate_per_hour'], item['unit'])} per hour{note}"
                ),
                provider=item["provider"],
                evidence={
                    "hours_to_zero": hours,
                    "value": item["value"],
                    "rate_per_hour": item["rate_per_hour"],
                    "unit": item["unit"],
                    "forecast": forecast,
                },
            )
        ]
    return []


def _spend_anomaly(path: Path, item: dict[str, Any]) -> list[Alert]:
    """Compare a provider's current burn with its own recent baseline.

    An absolute threshold cannot serve a table holding both $4/hour and 15 000
    credits/hour, and a top-up is not spend, so the comparison is made on the same
    robust rate the dashboard shows rather than on raw deltas.
    """

    if item["pay_model"] == "spend_report":
        return []
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            """
            SELECT observed_at, value FROM observations
            WHERE provider = ? AND observed_at >= ?
            ORDER BY id
            """,
            (
                item["provider"],
                (datetime.now(UTC) - timedelta(hours=SPEND_BASELINE_HOURS)).isoformat(),
            ),
        ).fetchall()
    if len(rows) < SPEND_MIN_BASELINE_POINTS:
        return []
    cutoff = _parse_time(rows[-1][0]) - timedelta(minutes=SPEND_RECENT_MINUTES)
    baseline_points = [(at, value) for at, value in rows if _parse_time(at) < cutoff]
    recent_points = [(at, value) for at, value in rows if _parse_time(at) >= cutoff]
    baseline = _rate_per_hour(baseline_points)
    recent = _rate_per_hour(recent_points)
    if not baseline or not recent or baseline >= 0 or recent >= 0:
        return []
    if abs(recent) < abs(baseline) * SPEND_ANOMALY_FACTOR:
        return []
    return [
        Alert(
            key=f"spend_anomaly:{item['provider']}",
            rule="spend_anomaly",
            severity=TODAY,
            text=(
                f"{item['name']} is spending {abs(recent) / abs(baseline):.1f}x its own baseline: "
                f"{_fmt(recent, item['unit'])} per hour over the last "
                f"{SPEND_RECENT_MINUTES} minutes against {_fmt(baseline, item['unit'])} "
                f"per hour before that"
            ),
            provider=item["provider"],
            evidence={
                "recent_rate_per_hour": recent,
                "baseline_rate_per_hour": baseline,
                "recent_minutes": SPEND_RECENT_MINUTES,
                "baseline_hours": SPEND_BASELINE_HOURS,
                "unit": item["unit"],
            },
        )
    ]


def collector_alerts(path: Path, now: datetime) -> list[Alert]:
    """The one rule the collector cannot raise about itself.

    A dead poller writes nothing, including alerts, so this is evaluated by the
    dashboard process. It stays silent until at least one cycle has ever run,
    because an empty database means "not started", not "died".
    """

    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT max(requested_at) FROM raw_responses").fetchone()
    if not row or not row[0]:
        return []
    silence = (now - _parse_time(row[0])).total_seconds()
    if silence < COLLECTOR_SILENCE_SECONDS:
        return []
    return [
        Alert(
            key="collector_dead",
            rule="collector_dead",
            severity=PAGE,
            text=f"No polling cycle for {silence / 60:.1f} minutes: collection has stopped",
            evidence={"last_cycle_at": row[0], "silence_seconds": round(silence, 1)},
        )
    ]


def _write_line(journal: Path, payload: dict[str, Any]) -> None:
    """Append one alert transition as a single write, so a reader never sees half."""

    journal.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    handle = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(handle, line.encode("utf-8"))
    finally:
        os.close(handle)


def reconcile(
    path: Path,
    journal: Path,
    alerts: list[Alert],
    now: datetime,
    resolves: frozenset[str] | None = None,
) -> dict[str, int]:
    """Open what became true, resolve what stopped being true, repeat nothing.

    ``resolves`` names the rules this caller is responsible for. Anything else that
    is open stays open: a writer must not close a condition it cannot evaluate.
    """

    initialise_database(path)
    stamp = now.isoformat()
    opened = resolved = 0
    with sqlite3.connect(path, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        current = {
            row["dedupe_key"]: row
            for row in connection.execute(
                "SELECT * FROM alerts WHERE resolved_at IS NULL"
            ).fetchall()
        }
        wanted = {alert.key: alert for alert in alerts}

        for key, alert in wanted.items():
            if key in current:
                continue
            connection.execute(
                """
                INSERT INTO alerts (
                    emitted_at, provider, rule, severity, text, evidence_json, dedupe_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stamp,
                    alert.provider,
                    alert.rule,
                    alert.severity,
                    alert.text,
                    json.dumps(alert.evidence, ensure_ascii=False, sort_keys=True),
                    key,
                ),
            )
            opened += 1
            _write_line(
                journal,
                {
                    "at": stamp,
                    "event": "opened",
                    "rule": alert.rule,
                    "severity": alert.severity,
                    "provider": alert.provider,
                    "text": alert.text,
                    "evidence": alert.evidence,
                    "dedupe_key": key,
                },
            )

        for key, row in current.items():
            if key in wanted:
                continue
            if resolves is not None and row["rule"] not in resolves:
                continue
            connection.execute(
                "UPDATE alerts SET resolved_at = ? WHERE id = ?", (stamp, row["id"])
            )
            resolved += 1
            _write_line(
                journal,
                {
                    "at": stamp,
                    "event": "resolved",
                    "rule": row["rule"],
                    "severity": row["severity"],
                    "provider": row["provider"],
                    "text": row["text"],
                    "opened_at": row["emitted_at"],
                    "dedupe_key": key,
                },
            )
    return {"opened": opened, "resolved": resolved, "open": len(wanted)}


def open_keys(path: Path) -> set[str]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        try:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT dedupe_key FROM alerts WHERE resolved_at IS NULL"
                )
            }
        except sqlite3.OperationalError:
            return set()


def open_alerts(path: Path) -> list[dict[str, Any]]:
    """Open alerts, or nothing at all for a database written before this policy.

    The read surface is also pointed at archived databases, and an archive from
    before alerting simply has no alerts to show; that is not an error.
    """

    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT emitted_at, provider, rule, severity, text, evidence_json, dedupe_key
                FROM alerts WHERE resolved_at IS NULL ORDER BY emitted_at DESC
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    order = {PAGE: 0, TODAY: 1, FYI: 2}
    items = [
        {
            "at": row["emitted_at"],
            "provider": row["provider"],
            "rule": row["rule"],
            "severity": row["severity"],
            "text": row["text"],
            "evidence": json.loads(row["evidence_json"]),
            "dedupe_key": row["dedupe_key"],
        }
        for row in rows
    ]
    return sorted(items, key=lambda item: (order.get(item["severity"], 3), item["at"]))


def recent_alerts(path: Path, hours: int = 12) -> list[dict[str, Any]]:
    """Alerts that opened within the window, resolved ones included.

    An alert that came and went is the most useful thing to look at once it is
    gone, and it is exactly what the open list can no longer show.
    """

    cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT emitted_at, resolved_at, provider, rule, severity, text, dedupe_key
                FROM alerts WHERE emitted_at >= ? ORDER BY emitted_at DESC LIMIT 200
                """,
                (cutoff,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [
        {
            "at": row["emitted_at"],
            "resolved_at": row["resolved_at"],
            "provider": row["provider"],
            "rule": row["rule"],
            "severity": row["severity"],
            "text": row["text"],
            "dedupe_key": row["dedupe_key"],
        }
        for row in rows
    ]


def evaluate_and_store(path: Path, journal: Path, now: datetime | None = None) -> dict[str, int]:
    """Evaluate the provider rules over the current projection and reconcile."""

    now = now or datetime.now(UTC)
    overview = build_overview(path, hours=2, now=now)
    alerts = evaluate(overview, open_keys(path), path)
    return reconcile(path, journal, alerts, now, resolves=PROVIDER_RULES)
