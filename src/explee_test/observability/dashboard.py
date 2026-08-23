"""Read-only dashboard projections over canonical observations."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from explee_test.observability.adapters import PROVIDERS, ProviderDefinition
from explee_test.observability.events import detect_outages, detect_series_events


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _category(outcome: str, error_code: str | None) -> str:
    if outcome == "success":
        return "success"
    if outcome == "throttled" or error_code == "http_429":
        return "rate_limited"
    if error_code and error_code.startswith("http_5"):
        return "http_5xx"
    if outcome == "http_error":
        return "http_other"
    if outcome == "transport_error":
        return "transport"
    if outcome in {"empty_payload", "invalid_json"}:
        return "payload"
    return "normalization"


def _series_name(metric_name: str, labels_json: str) -> str:
    labels = json.loads(labels_json)
    suffix = " · ".join(labels.values())
    return f"{metric_name} · {suffix}" if suffix else metric_name


def _is_primary(definition: ProviderDefinition, labels_json: str) -> bool:
    if definition.pay_model != "spend_report":
        return True
    return json.loads(labels_json).get("window") == "trailing_24h"


RATE_LAG_SECONDS = 600.0
RATE_WINDOW_POINTS = 121


def _despike(points: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Rolling median of three: drops single-sample glitches, keeps real steps.

    A provider that reports one impossible sample and returns to its previous level
    on the next poll (meta_ads does this at every recomputation boundary) must not
    move a rate estimate. A genuine top-up persists across samples, and a median
    filter preserves such an edge instead of smearing it.
    """

    if len(points) < 3:
        return list(points)
    smoothed = [points[0]]
    for previous, current, following in zip(points, points[1:], points[2:], strict=False):
        smoothed.append(
            (current[0], statistics.median((previous[1], current[1], following[1])))
        )
    smoothed.append(points[-1])
    return smoothed


def _rate_per_hour(
    points: list[tuple[str, float]], lag_seconds: float = RATE_LAG_SECONDS
) -> float | None:
    """Median slope measured across a fixed time lag.

    Adjacent-sample slopes are quantised to zero whenever a provider changes its
    value less often than we poll: bounceban moved on 22% of samples, so the median
    adjacent slope was exactly 0 while it was burning 25.7 credits per hour, and the
    dashboard reported no spend and no runway. Comparing points a fixed lag apart
    makes the change observable, while taking the median over many overlapping pairs
    keeps the estimate robust to isolated top-ups, resets, and response gaps.
    """

    if len(points) < 2:
        return None
    series = _despike(points[-RATE_WINDOW_POINTS:])
    stamps = [_parse_time(at).timestamp() for at, _ in series]
    values = [value for _, value in series]

    rates = []
    right = 1
    for left in range(len(series)):
        right = max(right, left + 1)
        while right < len(series) and stamps[right] - stamps[left] < lag_seconds:
            right += 1
        if right >= len(series):
            break
        elapsed = stamps[right] - stamps[left]
        if elapsed <= 3 * lag_seconds:
            rates.append((values[right] - values[left]) * 3600 / elapsed)
    if not rates:
        # The window is shorter than one lag; use the widest pair it does contain.
        elapsed = stamps[-1] - stamps[0]
        if elapsed <= 0:
            return None
        rates = [(values[-1] - values[0]) * 3600 / elapsed]
    return round(statistics.median(rates), 4)


def _window_hours(labels_json: str) -> float | None:
    """Length of a spend window label such as 'trailing_24h' or 'trailing_30d'."""

    window = json.loads(labels_json).get("window")
    if not isinstance(window, str):
        return None
    match = re.fullmatch(r"trailing_(\d+)([hd])", window)
    if not match:
        return None
    amount = int(match.group(1))
    return float(amount if match.group(2) == "h" else amount * 24)


def _robust_level(points: list[tuple[str, float]], span: int = 5) -> float | None:
    """Median of the last few samples; a single-sample glitch cannot move it."""

    values = [value for _, value in points[-span:]]
    return statistics.median(values) if values else None


def _spend_per_hour(points: list[tuple[str, float]], window_hours: float | None) -> float | None:
    """Average outflow implied by a trailing-window spend total.

    A spend report is a cumulative total over a moving window, not a balance. Its
    slope measures how the window itself moves, so the burn rate is the level
    divided by the window length. The level is taken robustly because these feeds
    emit isolated glitch samples at recomputation boundaries.
    """

    if not window_hours:
        return None
    level = _robust_level(points)
    return round(level / window_hours, 4) if level is not None else None


def _window_outflow(points: list[tuple[str, float]]) -> float:
    """Money actually spent across the window, which is not the net change.

    A provider that burned 400 and was topped up by 300 spent 400, not 100. Summing
    only the falls answers "how much went out"; the difference against the net
    change is what a top-up contributed. Single-sample glitches are filtered first,
    so a source that reports one impossible value does not invent spend.
    """

    values = [value for _, value in _despike(points)]
    return sum(min(0.0, right - left) for left, right in zip(values, values[1:], strict=False))


def _rate_window_minutes(points: list[tuple[str, float]]) -> float | None:
    """How much time the rate estimate actually covered, which the reader must see."""

    used = points[-RATE_WINDOW_POINTS:]
    if len(used) < 2:
        return None
    return round((_parse_time(used[-1][0]) - _parse_time(used[0][0])).total_seconds() / 60, 1)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 1)


AT_RISK_HOURS = 48.0


def _forecast(
    definition: ProviderDefinition,
    value: float,
    rate: float | None,
    refresh_at: str | None,
    now: datetime,
) -> dict[str, Any] | None:
    """One comparable answer to 'when does this run out', per pay model.

    Every branch reports the same quantity where it exists: hours until the
    resource reaches zero. A package that survives until its refresh date reports
    what will be left instead, clamped at zero, because negative credits are not a
    thing a provider can hand back.
    """

    if definition.pay_model == "spend_report":
        if not rate:
            return None
        return {"kind": "projected_30d", "value": round(rate * 24 * 30, 2)}
    if value is not None and value < 0:
        return {"kind": "in_debt", "value": round(value, 2)}
    if rate is None or rate >= 0 or value is None:
        return None
    hours_to_zero = value / -rate
    if definition.pay_model in {"prepaid_balance", "postpaid"}:
        return {"kind": "runway", "hours": round(hours_to_zero, 1)}
    if definition.pay_model == "credits_package":
        if not refresh_at:
            return {"kind": "runway", "hours": round(hours_to_zero, 1)}
        refresh = datetime.fromisoformat(refresh_at).replace(tzinfo=UTC)
        hours_to_refresh = max(0.0, (refresh - now).total_seconds() / 3600)
        if hours_to_zero < hours_to_refresh:
            return {
                "kind": "before_refresh",
                "hours": round(hours_to_zero, 1),
                "refresh_at": refresh_at,
                "refresh_hours": round(hours_to_refresh, 1),
            }
        return {
            "kind": "refresh_first",
            "value": round(max(0.0, value + rate * hours_to_refresh), 1),
            "refresh_at": refresh_at,
            "refresh_hours": round(hours_to_refresh, 1),
        }
    return None


def _risk_hours(forecast: dict[str, Any] | None) -> float | None:
    """Hours until exhaustion, comparable across pay models; None when not at risk."""

    if not forecast:
        return None
    if forecast["kind"] == "in_debt":
        return 0.0
    if forecast["kind"] in {"runway", "before_refresh"}:
        return forecast["hours"]
    return None


def _relative_rate(rate: float | None, value: float | None) -> float | None:
    """Rate as a share of the current level, so unlike units can be ranked together."""

    if rate is None or not value:
        return None
    return round(100 * rate / abs(value), 3)


def _sparkline(points: list[tuple[str, float]], width: int = 48) -> list[float]:
    if not points:
        return []
    stride = max(1, len(points) // width)
    return [round(value, 4) for _, value in points[::stride]][-width:]


def _cycle_outcomes(rows: list[sqlite3.Row]) -> list[tuple[str, str, str | None]]:
    """Collapse a provider's attempts into one entry per polling cycle.

    Outages are a statement about polls that came back empty-handed, not about
    individual requests: a cycle whose retry succeeded delivered its data.
    """

    order: list[str] = []
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        cycle = row["cycle_id"]
        if cycle not in seen:
            order.append(cycle)
            seen[cycle] = {"at": row["requested_at"], "outcome": "no_data", "categories": []}
        entry = seen[cycle]
        if row["outcome"] == "success":
            entry["outcome"] = "success"
        else:
            entry["categories"].append(_category(row["outcome"], row["error_code"]))
    collapsed = []
    for cycle in order:
        entry = seen[cycle]
        categories = entry["categories"]
        dominant = max(set(categories), key=categories.count) if categories else None
        collapsed.append((entry["at"], entry["outcome"], dominant))
    return collapsed


def _open_alerts(path: Path) -> list[dict[str, Any]]:
    """Imported lazily: policy reads this projection, so it cannot be imported at top."""

    from explee_test.observability.alerts import open_alerts

    return open_alerts(path)


def _recent_alerts(path: Path, hours: float) -> list[dict[str, Any]]:
    from explee_test.observability.alerts import recent_alerts

    return recent_alerts(path, max(1, round(hours)))


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _window(
    hours: int, now: datetime, start: str | None, end: str | None
) -> tuple[str, str, float]:
    """Resolve the observation window: either a preset span or an explicit range.

    An explicit range is what a click on a chart produces, and every panel has to
    agree on it, so it is resolved once here rather than per query.
    """

    if start:
        begin = _parse_time(start)
        finish = _parse_time(end) if end else now
        span = max((finish - begin).total_seconds() / 3600, 1 / 60)
        return begin.isoformat(), finish.isoformat(), span
    return (now - timedelta(hours=hours)).isoformat(), now.isoformat(), float(hours)


def build_overview(
    path: Path,
    hours: int,
    now: datetime | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Build the one-glance provider and observation-quality projection."""

    now = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff, until, span_hours = _window(hours, now, start, end)
    with _connect(path) as connection:
        attempts = connection.execute(
            """
            SELECT r.provider, r.requested_at, r.latency_ms, r.status_code,
                   r.cycle_id, p.outcome, p.error_code
            FROM processing_results AS p
            JOIN raw_responses AS r ON r.id = p.raw_response_id
            WHERE r.requested_at >= ? AND r.requested_at <= ?
            ORDER BY r.requested_at
            """,
            (cutoff, until),
        ).fetchall()
        latest_attempt_rows = connection.execute(
            """
            WITH ranked AS (
                SELECT r.provider, r.requested_at, r.latency_ms, r.status_code,
                       p.outcome, p.error_code,
                       row_number() OVER (PARTITION BY r.provider ORDER BY r.id DESC) AS rank
                FROM processing_results AS p
                JOIN raw_responses AS r ON r.id = p.raw_response_id
            )
            SELECT * FROM ranked WHERE rank = 1
            """
        ).fetchall()
        observation_rows = connection.execute(
            """
            SELECT provider, observed_at, metric_name, value, capacity, unit,
                   refresh_at, labels_json, raw_response_id
            FROM observations
            WHERE observed_at >= ? AND observed_at <= ?
            ORDER BY observed_at
            """,
            (cutoff, until),
        ).fetchall()
        latest_observation_rows = connection.execute(
            """
            WITH ranked AS (
                SELECT *, row_number() OVER (
                    PARTITION BY provider, metric_name, labels_json ORDER BY id DESC
                ) AS rank
                FROM observations
            )
            SELECT * FROM ranked WHERE rank = 1
            """
        ).fetchall()
        streak_rows = connection.execute(
            """
            SELECT r.provider, r.requested_at, p.outcome
            FROM processing_results AS p
            JOIN raw_responses AS r ON r.id = p.raw_response_id
            ORDER BY r.provider, r.id DESC
            """
        ).fetchall()

    attempts_by_provider: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in attempts:
        attempts_by_provider[row["provider"]].append(row)
    # A cycle is what the reader cares about: did this poll produce data at all?
    # A throttled attempt followed by a successful retry in the same cycle is not a
    # collection failure, so it must not be counted or drawn as one.
    cycles: dict[str, set[str]] = defaultdict(set)
    covered: set[tuple[str, str]] = set()
    for row in attempts:
        cycles[row["provider"]].add(row["cycle_id"])
        if row["outcome"] == "success":
            covered.add((row["provider"], row["cycle_id"]))

    def recovered(row: sqlite3.Row) -> bool:
        return (
            row["outcome"] == "throttled" and (row["provider"], row["cycle_id"]) in covered
        )
    history: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in observation_rows:
        definition = PROVIDERS[row["provider"]]
        if _is_primary(definition, row["labels_json"]):
            history[row["provider"]].append(row)
    latest_attempt = {row["provider"]: row for row in latest_attempt_rows}
    latest_observation = {}
    for row in latest_observation_rows:
        definition = PROVIDERS[row["provider"]]
        if _is_primary(definition, row["labels_json"]):
            latest_observation[row["provider"]] = row

    streaks: dict[str, dict[str, Any]] = {}
    grouped_streaks: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in streak_rows:
        grouped_streaks[row["provider"]].append(row)
    for provider, rows in grouped_streaks.items():
        failed = []
        for row in rows:
            if row["outcome"] == "success":
                break
            failed.append(row)
        duration = 0.0
        if len(failed) > 1:
            duration = (
                _parse_time(failed[0]["requested_at"])
                - _parse_time(failed[-1]["requested_at"])
            ).total_seconds()
        streaks[provider] = {"count": len(failed), "duration_seconds": duration}

    providers = []
    events = []
    for provider, definition in PROVIDERS.items():
        provider_attempts = attempts_by_provider[provider]
        successes = sum(row["outcome"] == "success" for row in provider_attempts)
        provider_cycles = cycles[provider]
        covered_cycles = {cycle for name, cycle in covered if name == provider}
        # Consecutive most recent cycles that produced nothing: the quantity an
        # alert should reason about, because it counts polls rather than requests.
        missing_cycles = 0
        for _, outcome, _ in reversed(_cycle_outcomes(provider_attempts)):
            if outcome == "success":
                break
            missing_cycles += 1
        counts: dict[str, int] = defaultdict(int)
        for row in provider_attempts:
            if recovered(row):
                continue
            counts[_category(row["outcome"], row["error_code"])] += 1
        latest = latest_observation.get(provider)
        points = [(row["observed_at"], row["value"]) for row in history[provider]]
        traced = [
            (row["observed_at"], row["value"], row["raw_response_id"]) for row in history[provider]
        ]
        value = latest["value"] if latest else None
        window_hours = _window_hours(latest["labels_json"]) if latest else None
        if definition.pay_model == "spend_report":
            rate = _spend_per_hour(points, window_hours)
            window_delta_per_hour = _rate_per_hour(points)
        else:
            rate = _rate_per_hour(points)
            window_delta_per_hour = None
        forecast = (
            _forecast(definition, value, rate, latest["refresh_at"], now) if latest else None
        )
        events.extend(
            detect_series_events(
                provider,
                definition.pay_model,
                latest["unit"] if latest else definition.unit,
                traced,
                latest["capacity"] if latest else None,
            )
        )
        events.extend(detect_outages(provider, _cycle_outcomes(provider_attempts)))
        freshness = (
            (now - _parse_time(latest["observed_at"])).total_seconds() if latest else None
        )
        providers.append(
            {
                "provider": provider,
                "name": definition.name,
                "pay_model": definition.pay_model,
                "unit": latest["unit"] if latest else definition.unit,
                "value": value,
                "capacity": latest["capacity"] if latest else None,
                "refresh_at": latest["refresh_at"] if latest else None,
                "observed_at": latest["observed_at"] if latest else None,
                "freshness_seconds": round(freshness, 1) if freshness is not None else None,
                "rate_per_hour": rate,
                "rate_kind": (
                    "spend" if definition.pay_model == "spend_report" else "balance_change"
                ),
                "spend_window_hours": window_hours,
                "window_delta_per_hour": window_delta_per_hour,
                "forecast": forecast,
                "risk_hours": _risk_hours(forecast),
                "rate_percent_per_hour": (
                    None
                    if definition.pay_model == "spend_report"
                    else _relative_rate(rate, value)
                ),
                "sparkline": _sparkline(points),
                "rate_window_minutes": _rate_window_minutes(points),
                "window_spend": (
                    round(abs(rate) * span_hours, 4)
                    if definition.pay_model == "spend_report" and rate
                    else round(abs(_window_outflow(points)), 4)
                ),
                "window_net": (
                    round(points[-1][1] - points[0][1], 4) if len(points) > 1 else None
                ),
                "attempts": len(provider_attempts),
                "valid_percent": round(100 * successes / len(provider_attempts), 1)
                if provider_attempts
                else None,
                "data_percent": round(
                    100 * len(covered_cycles) / len(provider_cycles), 1
                )
                if provider_cycles
                else None,
                "cycles": len(provider_cycles),
                "missing_cycles": missing_cycles,
                "throttled_recovered": sum(recovered(row) for row in provider_attempts),
                "outcomes": dict(counts),
                "last_attempt": dict(latest_attempt[provider])
                if provider in latest_attempt
                else None,
                "failure_streak": streaks.get(provider, {"count": 0, "duration_seconds": 0}),
            }
        )

    bucket_seconds = 60 if span_hours <= 2 else 300 if span_hours <= 24 else 3600
    buckets: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    bucket_latencies: dict[int, list[float]] = defaultdict(list)
    latencies = []
    for row in attempts:
        if recovered(row):
            continue
        timestamp = int(_parse_time(row["requested_at"]).timestamp())
        bucket = timestamp - timestamp % bucket_seconds
        category = _category(row["outcome"], row["error_code"])
        buckets[bucket][category] += 1
        if row["latency_ms"] is not None:
            latencies.append(row["latency_ms"])
            bucket_latencies[bucket].append(row["latency_ms"])
    quality_series = []
    for timestamp, counts in sorted(buckets.items()):
        total = sum(counts.values())
        quality_series.append(
            {
                "at": datetime.fromtimestamp(timestamp, UTC).isoformat(),
                "total": total,
                "valid_percent": round(100 * counts.get("success", 0) / total, 1),
                "p95_latency_ms": _percentile(bucket_latencies[timestamp], 0.95),
                **counts,
            }
        )

    counted = [row for row in attempts if not recovered(row)]
    total = len(counted)
    valid = sum(row["outcome"] == "success" for row in counted)
    # Only USD-denominated outflow can be summed honestly; credits have no price
    # here and the single GBP balance is not converted without a rate we can cite.
    comparable = [
        item
        for item in providers
        if item["unit"] == "usd" and item["rate_per_hour"] is not None
    ]
    burn = sum(
        item["rate_per_hour"] if item["pay_model"] == "spend_report" else -item["rate_per_hour"]
        for item in comparable
        if item["pay_model"] == "spend_report" or item["rate_per_hour"] < 0
    )
    # "Already below zero" and "will reach zero soon" are different states, and
    # folding them into one count reads as though nothing has happened yet.
    in_debt = [
        item["provider"] for item in providers if (item["forecast"] or {}).get("kind") == "in_debt"
    ]
    covered_hours = 0.0
    if attempts:
        covered_hours = (
            _parse_time(attempts[-1]["requested_at"]) - _parse_time(attempts[0]["requested_at"])
        ).total_seconds() / 3600
    spent = sum(
        item["window_spend"]
        for item in providers
        if item["unit"] == "usd" and item["window_spend"]
    )
    rate_windows = [
        item["rate_window_minutes"] for item in providers if item["rate_window_minutes"]
    ]
    at_risk = [
        item["provider"]
        for item in providers
        if item["provider"] not in in_debt
        and item["risk_hours"] is not None
        and item["risk_hours"] <= AT_RISK_HOURS
    ]
    fresh = sum(
        item["freshness_seconds"] is not None and item["freshness_seconds"] <= 90
        for item in providers
    )
    degraded = sum(
        item["last_attempt"] is not None
        and item["last_attempt"]["outcome"] not in {"success", "throttled"}
        for item in providers
    )
    return {
        "generated_at": now.isoformat(),
        "range_hours": round(span_hours, 4),
        "range_start": cutoff,
        "range_end": until,
        "summary": {
            "providers": len(PROVIDERS),
            "fresh": fresh,
            "degraded": degraded,
            "attempts": total,
            "throttled_recovered": len(attempts) - total,
            "valid_percent": round(100 * valid / total, 1) if total else None,
            "p95_latency_ms": _percentile(latencies, 0.95),
            "usd_burn_per_hour": round(burn, 4),
            "usd_projected_30d": round(burn * 24 * 30, 2),
            "usd_sources": len(comparable),
            # The rate is a speedometer over its own short window; the selected
            # range scopes everything else, and the two must be told apart.
            "rate_window_minutes": round(statistics.median(rate_windows), 1)
            if rate_windows
            else None,
            "window_spend_usd": round(spent, 2),
            # Averaging over a window the data does not fill would understate the
            # rate; right after a restart the two differ a lot, so both are shown.
            "window_covered_hours": round(covered_hours, 3),
            "window_spend_per_hour": round(spent / covered_hours, 4) if covered_hours else None,
            "at_risk": len(at_risk),
            "at_risk_providers": at_risk,
            "in_debt": len(in_debt),
            "in_debt_providers": in_debt,
            "at_risk_hours": AT_RISK_HOURS,
            "events": len(events),
        },
        "providers": providers,
        "alerts": _open_alerts(path),
        "alerts_recent": _recent_alerts(path, span_hours),
        "quality_series": quality_series,
        "events": [
            event.as_dict() for event in sorted(events, key=lambda item: item.at, reverse=True)
        ],
    }


def build_provider_detail(
    path: Path,
    provider: str,
    hours: int,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Return canonical series, processing outcomes, and events for one provider."""

    if provider not in PROVIDERS:
        raise KeyError(provider)
    cutoff, until, _ = _window(hours, datetime.now(UTC), start, end)
    with _connect(path) as connection:
        observations = connection.execute(
            """
            SELECT observed_at, metric_name, value, capacity, unit, refresh_at,
                   labels_json, raw_response_id
            FROM observations
            WHERE provider = ? AND observed_at >= ? AND observed_at <= ?
            ORDER BY observed_at
            """,
            (provider, cutoff, until),
        ).fetchall()
        attempts = connection.execute(
            """
            SELECT r.requested_at, r.status_code, r.latency_ms, r.cycle_id, r.attempt,
                   p.outcome, p.error_code
            FROM processing_results AS p
            JOIN raw_responses AS r ON r.id = p.raw_response_id
            WHERE r.provider = ? AND r.requested_at >= ? AND r.requested_at <= ?
            ORDER BY r.requested_at
            """,
            (provider, cutoff, until),
        ).fetchall()
        latest_raw = connection.execute(
            """
            SELECT r.id, r.requested_at, r.body_text
            FROM processing_results AS p
            JOIN raw_responses AS r ON r.id = p.raw_response_id
            WHERE r.provider = ? AND p.outcome = 'success'
            ORDER BY r.id DESC LIMIT 1
            """,
            (provider,),
        ).fetchone()

    series: dict[str, dict[str, Any]] = {}
    for row in observations:
        name = _series_name(row["metric_name"], row["labels_json"])
        item = series.setdefault(
            name,
            {"name": name, "unit": row["unit"], "points": []},
        )
        item["points"].append([row["observed_at"], row["value"], row["raw_response_id"]])
    definition = PROVIDERS[provider]
    recovered_cycles = {row["cycle_id"] for row in attempts if row["outcome"] == "success"}
    primary = [
        (row["observed_at"], row["value"], row["raw_response_id"])
        for row in observations
        if _is_primary(definition, row["labels_json"])
    ]
    events = detect_series_events(
        provider,
        definition.pay_model,
        definition.unit,
        primary,
        observations[-1]["capacity"] if observations else None,
    )
    events.extend(detect_outages(provider, _cycle_outcomes(attempts)))
    return {
        "events": [event.as_dict() for event in sorted(events, key=lambda item: item.at)],
        "provider": {
            "provider": definition.provider,
            "name": definition.name,
            "pay_model": definition.pay_model,
            "unit": definition.unit,
        },
        "series": list(series.values()),
        "attempts": [
            {
                "at": row["requested_at"],
                "category": (
                    "throttled_recovered"
                    if row["outcome"] == "throttled" and row["cycle_id"] in recovered_cycles
                    else _category(row["outcome"], row["error_code"])
                ),
                "outcome": row["outcome"],
                "status_code": row["status_code"],
                "attempt": row["attempt"],
                "latency_ms": row["latency_ms"],
            }
            for row in attempts
        ],
        "latest_raw": {
            "raw_response_id": latest_raw["id"],
            "requested_at": latest_raw["requested_at"],
            "payload": json.loads(latest_raw["body_text"]),
        }
        if latest_raw
        else None,
    }


def get_raw_response(path: Path, raw_response_id: int) -> dict[str, Any]:
    """Return one stored response so any plotted point can be traced to its source.

    Only the fields the dashboard already publishes are exposed: no headers, no
    URLs, no transport error text.
    """

    with _connect(path) as connection:
        row = connection.execute(
            """
            SELECT r.id, r.provider, r.requested_at, r.status_code, r.latency_ms,
                   r.body_text, r.body_is_json, p.outcome, p.error_code
            FROM raw_responses AS r
            LEFT JOIN processing_results AS p ON p.raw_response_id = r.id
            WHERE r.id = ?
            """,
            (raw_response_id,),
        ).fetchone()
    if row is None:
        raise KeyError(raw_response_id)
    payload: Any = None
    if row["body_is_json"]:
        try:
            payload = json.loads(row["body_text"])
        except (TypeError, json.JSONDecodeError):
            payload = None
    return {
        "raw_response_id": row["id"],
        "provider": row["provider"],
        "requested_at": row["requested_at"],
        "status_code": row["status_code"],
        "latency_ms": row["latency_ms"],
        "outcome": row["outcome"],
        "error_code": row["error_code"],
        "payload": payload,
        "body_is_json": bool(row["body_is_json"]),
    }
