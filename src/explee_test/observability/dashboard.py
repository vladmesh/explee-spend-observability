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
# The rate is a speedometer: it always reads the most recent samples, whatever range
# the reader has selected. Two hours is comfortably more than RATE_WINDOW_POINTS polls.
RATE_LOOKBACK_HOURS = 2.0
# Tails read per provider instead of scanning the whole capture. Both are far longer
# than the handful of rows the answer actually needs, and both are bounded, which is
# the point: the cost of a page view must not grow with how long the collector ran.
LATEST_OBSERVATION_TAIL = 60
STREAK_TAIL = 1000


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


# `_category` as SQL, so a window's attempts can be counted where they live instead
# of being carried into this process one row at a time. The order of the branches is
# the order of the Python function and has to stay that way: a throttled response is
# a stated delay first and a 429 second.
CATEGORY_SQL = """
    CASE
        WHEN p.outcome = 'success' THEN 'success'
        WHEN p.outcome = 'throttled' OR p.error_code = 'http_429' THEN 'rate_limited'
        WHEN p.error_code LIKE 'http_5%' THEN 'http_5xx'
        WHEN p.outcome = 'http_error' THEN 'http_other'
        WHEN p.outcome = 'transport_error' THEN 'transport'
        WHEN p.outcome IN ('empty_payload', 'invalid_json') THEN 'payload'
        ELSE 'normalization'
    END
"""

# The rank of the sample that `_percentile` would pick, expressed for SQLite:
# ceil(q * n) clamped into [1, n], which is the 1-based form of its index.
def _percentile_rank_sql(quantile: float) -> str:
    return (
        f"min(n, max(1, CAST({quantile} * n AS INTEGER) "
        f"+ (CASE WHEN {quantile} * n > CAST({quantile} * n AS INTEGER) THEN 1 ELSE 0 END)))"
    )


def _rounded(rows) -> dict[int, float]:
    return {bucket: round(value, 1) for bucket, value in rows}


def _load_window_attempts(connection: sqlite3.Connection, cutoff: str, until: str) -> None:
    """Materialise the window's attempts once, then answer every count from it.

    Every summary the page shows about collection is a count, a share or a
    percentile over the same set of rows. Fetching that set into Python and looping
    over it cost seconds on a week-long window; the set is built here as temporary
    tables so the aggregates below read it in SQLite and hand back tens of rows.
    """

    connection.executescript(
        """
        DROP VIEW IF EXISTS temp.counted;
        DROP TABLE IF EXISTS temp.cyc;
        DROP TABLE IF EXISTS temp.dominant;
        DROP TABLE IF EXISTS temp.att;
        """
    )
    # `covered` travels with every attempt: whether the cycle it belongs to delivered
    # data in the end. It is what tells a throttled attempt that a retry rescued from
    # one that lost the poll, and computing it here saves a second pass over the rows.
    connection.execute(
        f"""
        CREATE TEMP TABLE att AS
        SELECT provider, cycle_id, at, latency_ms, outcome, category,
               max(outcome = 'success') OVER (PARTITION BY provider, cycle_id) AS covered
        FROM (
            SELECT r.provider AS provider, r.cycle_id AS cycle_id, r.requested_at AS at,
                   r.latency_ms AS latency_ms, p.outcome AS outcome,
                   {CATEGORY_SQL} AS category
            FROM raw_responses AS r
            JOIN processing_results AS p ON p.raw_response_id = r.id
            WHERE r.requested_at >= ? AND r.requested_at <= ?
        )
        """,
        (cutoff, until),
    )
    # A throttled attempt that a retry rescued is not a collection failure, so every
    # count of collection quality reads this view rather than the table.
    connection.execute(
        "CREATE TEMP VIEW counted AS "
        "SELECT * FROM att WHERE NOT (outcome = 'throttled' AND covered = 1)"
    )
    # What mostly went wrong in a cycle, for the cycles where something did.
    connection.execute(
        """
        CREATE TEMP TABLE dominant AS
        SELECT provider, cycle_id, category FROM (
            SELECT provider, cycle_id, category,
                   row_number() OVER (
                       PARTITION BY provider, cycle_id ORDER BY count(*) DESC
                   ) AS rank
            FROM att
            WHERE outcome <> 'success'
            GROUP BY provider, cycle_id, category
        )
        WHERE rank = 1
        """
    )
    connection.execute("CREATE INDEX temp.dominant_cycle ON dominant(provider, cycle_id)")
    # One row per polling cycle: did this poll deliver data at all, and if not, what
    # mostly went wrong. A cycle whose retry succeeded delivered its data.
    connection.execute(
        """
        CREATE TEMP TABLE cyc AS
        SELECT grouped.provider AS provider, grouped.cycle_id AS cycle_id,
               grouped.at AS at, grouped.covered AS covered,
               dominant.category AS dominant
        FROM (
            SELECT provider, cycle_id, min(at) AS at, max(covered) AS covered
            FROM att GROUP BY provider, cycle_id
        ) AS grouped
        LEFT JOIN dominant
            ON dominant.provider = grouped.provider
           AND dominant.cycle_id = grouped.cycle_id
        """
    )
    connection.execute("CREATE INDEX temp.cyc_provider ON cyc(provider, at)")


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
        bucket_seconds = 60 if span_hours <= 2 else 300 if span_hours <= 24 else 3600
        bucket_sql = (
            f"CAST(strftime('%s', at) AS INTEGER) / {bucket_seconds} * {bucket_seconds}"
        )
        _load_window_attempts(connection, cutoff, until)
        provider_totals = {
            row["provider"]: row
            for row in connection.execute(
                """
                SELECT provider, count(*) AS attempts,
                       sum(outcome = 'success') AS successes
                FROM att GROUP BY provider
                """
            )
        }
        cycle_totals = {
            row["provider"]: row
            for row in connection.execute(
                "SELECT provider, count(*) AS cycles, sum(covered) AS covered "
                "FROM cyc GROUP BY provider"
            )
        }
        # Cycles since this provider last delivered anything. An empty string sorts
        # before every timestamp, so a provider that never succeeded counts them all.
        missing_cycles = dict(
            connection.execute(
                """
                SELECT provider, count(*) FROM cyc AS c
                WHERE c.at > coalesce(
                    (SELECT max(at) FROM cyc AS s
                     WHERE s.provider = c.provider AND s.covered = 1),
                    ''
                )
                GROUP BY provider
                """
            )
        )
        outcome_counts: dict[str, dict[str, int]] = defaultdict(dict)
        for provider, category, count in connection.execute(
            "SELECT provider, category, count(*) FROM counted GROUP BY provider, category"
        ):
            outcome_counts[provider][category] = count
        # `covered` already travels with the attempt, so this is a scan and not a
        # join: joining the two temporary tables had no index to work with and cost
        # a minute on a week-long window.
        throttled_recovered = dict(
            connection.execute(
                "SELECT provider, count(*) FROM att "
                "WHERE outcome = 'throttled' AND covered = 1 GROUP BY provider"
            )
        )
        # Only the cycles that delivered nothing come out, each carrying its position
        # among the cycles an outage can be built from. Consecutive positions are one
        # outage, so the successful cycles between them never have to be carried.
        outage_rows = connection.execute(
            """
            WITH kept AS (
                SELECT provider, at, covered, dominant,
                       row_number() OVER (PARTITION BY provider ORDER BY at) AS seq
                FROM cyc
                WHERE dominant IS NULL OR dominant <> 'rate_limited'
            )
            SELECT provider, at, dominant, seq FROM kept
            WHERE covered = 0
            ORDER BY provider, at
            """
        ).fetchall()
        bucket_rows = connection.execute(
            f"SELECT {bucket_sql} AS bucket, category, count(*) "
            "FROM counted GROUP BY bucket, category"
        ).fetchall()
        # Rounded here rather than in SQL: SQLite rounds a half away from zero and
        # Python rounds it to even, and the two must not disagree on a tenth.
        bucket_p95 = _rounded(
            connection.execute(
                f"""
                WITH bucketed AS (
                    SELECT {bucket_sql} AS bucket, latency_ms
                    FROM counted WHERE latency_ms IS NOT NULL
                ),
                ranked AS (
                    SELECT bucket, latency_ms,
                           row_number() OVER (PARTITION BY bucket ORDER BY latency_ms) AS rn,
                           count(*) OVER (PARTITION BY bucket) AS n
                    FROM bucketed
                )
                SELECT bucket, latency_ms FROM ranked
                WHERE rn = {_percentile_rank_sql(0.95)}
                """
            )
        )
        window_p95 = connection.execute(
            f"""
            WITH ranked AS (
                SELECT latency_ms,
                       row_number() OVER (ORDER BY latency_ms) AS rn,
                       count(*) OVER () AS n
                FROM counted WHERE latency_ms IS NOT NULL
            )
            SELECT latency_ms FROM ranked WHERE rn = {_percentile_rank_sql(0.95)}
            """
        ).fetchone()
        total, valid = connection.execute(
            "SELECT count(*), coalesce(sum(outcome = 'success'), 0) FROM counted"
        ).fetchone()
        window_attempts, first_at, last_at = connection.execute(
            "SELECT count(*), min(at), max(at) FROM att"
        ).fetchone()
        # Asked per provider over an index-ordered tail. Written as one ranked
        # scan of the whole table, this cost grew with every poll ever captured and
        # ignored the selected range entirely, which is what made the page slower
        # every day it ran.
        latest_attempt_rows = [
            row
            for provider in PROVIDERS
            for row in connection.execute(
                """
                SELECT r.provider, r.requested_at, r.latency_ms, r.status_code,
                       p.outcome, p.error_code
                FROM raw_responses AS r
                JOIN processing_results AS p ON p.raw_response_id = r.id
                WHERE r.provider = ?
                ORDER BY r.requested_at DESC, r.id DESC
                LIMIT 1
                """,
                (provider,),
            )
        ]
        # Only the columns the plotted series is made of. Asking for the whole row
        # sent the reader back to the table for every sample; these five live in the
        # index the range is scanned on.
        observation_rows = connection.execute(
            """
            SELECT provider, observed_at, value, raw_response_id, labels_json
            FROM observations
            WHERE observed_at >= ? AND observed_at <= ?
            ORDER BY observed_at
            """,
            (cutoff, until),
        ).fetchall()
        # A provider writes at most a couple of series per poll, so its newest
        # sample of each is inside a short tail; the ordering matches the
        # (provider, observed_at) index, so this reads a handful of rows.
        latest_observation_rows = [
            row
            for provider in PROVIDERS
            for row in connection.execute(
                """
                SELECT provider, observed_at, metric_name, value, capacity, unit,
                       refresh_at, labels_json, raw_response_id
                FROM observations
                WHERE provider = ?
                ORDER BY observed_at DESC, id DESC
                LIMIT ?
                """,
                (provider, LATEST_OBSERVATION_TAIL),
            )
        ]
        # Rates, forecasts and the burn headline answer "right now", so they read the
        # latest samples regardless of the selected range; a click that narrows the
        # window to five minutes must not turn them into five minutes of noise.
        rate_rows = connection.execute(
            """
            SELECT provider, observed_at, value, labels_json
            FROM observations
            WHERE observed_at >= ? AND observed_at <= ?
            ORDER BY observed_at
            """,
            ((now - timedelta(hours=RATE_LOOKBACK_HOURS)).isoformat(), now.isoformat()),
        ).fetchall()
        # Only the run of failures at the end of each provider's history matters,
        # and the whole history was being sorted to find it. A tail of this length
        # is several hours of polling; a streak that outlives it reads as its length.
        streak_rows = [
            row
            for provider in PROVIDERS
            for row in connection.execute(
                """
                SELECT r.provider, r.requested_at, p.outcome
                FROM raw_responses AS r
                JOIN processing_results AS p ON p.raw_response_id = r.id
                WHERE r.provider = ?
                ORDER BY r.requested_at DESC, r.id DESC
                LIMIT ?
                """,
                (provider, STREAK_TAIL),
            )
        ]

    # The failed cycles arrive without the successful ones between them, so a break
    # in their numbering is a recovery; `detect_outages` reads a run of failures the
    # same way whether the success that ended it is spelled out or implied.
    outage_cycles: dict[str, list[tuple[str, str, str | None]]] = defaultdict(list)
    previous: dict[str, int] = {}
    for row in outage_rows:
        provider = row["provider"]
        if provider in previous and row["seq"] != previous[provider] + 1:
            outage_cycles[provider].append((row["at"], "success", None))
        outage_cycles[provider].append((row["at"], "no_data", row["dominant"]))
        previous[provider] = row["seq"]

    history: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in observation_rows:
        definition = PROVIDERS[row["provider"]]
        if _is_primary(definition, row["labels_json"]):
            history[row["provider"]].append(row)
    recent: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for row in rate_rows:
        if _is_primary(PROVIDERS[row["provider"]], row["labels_json"]):
            recent[row["provider"]].append((row["observed_at"], row["value"]))
    latest_attempt = {row["provider"]: row for row in latest_attempt_rows}
    # The tail arrives newest first, so the first primary row a provider offers is
    # its current value and later rows are that provider's older samples.
    latest_observation = {}
    for row in latest_observation_rows:
        definition = PROVIDERS[row["provider"]]
        if _is_primary(definition, row["labels_json"]):
            latest_observation.setdefault(row["provider"], row)

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
        totals = provider_totals.get(provider)
        provider_attempts = totals["attempts"] if totals else 0
        successes = totals["successes"] if totals else 0
        provider_cycles = cycle_totals.get(provider)
        counts = outcome_counts.get(provider, {})
        latest = latest_observation.get(provider)
        points = [(row["observed_at"], row["value"]) for row in history[provider]]
        traced = [
            (row["observed_at"], row["value"], row["raw_response_id"]) for row in history[provider]
        ]
        value = latest["value"] if latest else None
        window_hours = _window_hours(latest["labels_json"]) if latest else None
        rate_points = recent[provider]
        if definition.pay_model == "spend_report":
            rate = _spend_per_hour(rate_points, window_hours)
            window_delta_per_hour = _rate_per_hour(rate_points)
        else:
            rate = _rate_per_hour(rate_points)
            window_delta_per_hour = None
        # A spend report has no balance to lose, so its window spend is the rate
        # over the hours the series actually covers, never over hours with no data.
        series_hours = (
            (_parse_time(points[-1][0]) - _parse_time(points[0][0])).total_seconds() / 3600
            if len(points) > 1
            else 0.0
        )
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
        events.extend(detect_outages(provider, outage_cycles[provider]))
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
                "rate_window_minutes": _rate_window_minutes(rate_points),
                "window_spend": (
                    round(abs(rate) * min(span_hours, series_hours), 4)
                    if definition.pay_model == "spend_report" and rate
                    else round(abs(_window_outflow(points)), 4)
                ),
                "window_net": (
                    round(points[-1][1] - points[0][1], 4) if len(points) > 1 else None
                ),
                "attempts": provider_attempts,
                "valid_percent": round(100 * successes / provider_attempts, 1)
                if provider_attempts
                else None,
                "data_percent": round(
                    100 * provider_cycles["covered"] / provider_cycles["cycles"], 1
                )
                if provider_cycles
                else None,
                "cycles": provider_cycles["cycles"] if provider_cycles else 0,
                "missing_cycles": missing_cycles.get(provider, 0),
                "throttled_recovered": throttled_recovered.get(provider, 0),
                "outcomes": dict(counts),
                "last_attempt": dict(latest_attempt[provider])
                if provider in latest_attempt
                else None,
                "failure_streak": streaks.get(provider, {"count": 0, "duration_seconds": 0}),
            }
        )

    buckets: dict[int, dict[str, int]] = defaultdict(dict)
    for timestamp, category, count in bucket_rows:
        buckets[timestamp][category] = count
    quality_series = []
    for timestamp, counts in sorted(buckets.items()):
        bucket_total = sum(counts.values())
        quality_series.append(
            {
                "at": datetime.fromtimestamp(timestamp, UTC).isoformat(),
                "total": bucket_total,
                "valid_percent": round(100 * counts.get("success", 0) / bucket_total, 1),
                "p95_latency_ms": bucket_p95.get(timestamp),
                **counts,
            }
        )

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
    if first_at and last_at:
        covered_hours = (
            _parse_time(last_at) - _parse_time(first_at)
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
            "throttled_recovered": window_attempts - total,
            "valid_percent": round(100 * valid / total, 1) if total else None,
            "p95_latency_ms": round(window_p95[0], 1) if window_p95 else None,
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
        # The columns the plotted series is made of, and no others: every column
        # asked for that the index does not carry sends the reader back to the table
        # once per sample. The capacity is a property of the newest sample alone, so
        # it is asked for separately rather than fetched with every one of them.
        observations = connection.execute(
            """
            SELECT observed_at, metric_name, value, unit, labels_json, raw_response_id
            FROM observations
            WHERE provider = ? AND observed_at >= ? AND observed_at <= ?
            ORDER BY observed_at
            """,
            (provider, cutoff, until),
        ).fetchall()
        capacity = connection.execute(
            """
            SELECT capacity FROM observations
            WHERE provider = ? AND observed_at >= ? AND observed_at <= ?
            ORDER BY observed_at DESC, id DESC LIMIT 1
            """,
            (provider, cutoff, until),
        ).fetchone()
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
        # The newest success is found by walking this provider's index backwards and
        # stopping at the first one. Left to itself the planner drove the join from
        # the outcome index instead, which collected every success ever recorded, for
        # every provider, and sorted them all to keep one row; CROSS JOIN is how
        # SQLite is told which table leads.
        latest_raw = connection.execute(
            """
            SELECT r.id, r.requested_at, r.body_text
            FROM raw_responses AS r
            CROSS JOIN processing_results AS p ON p.raw_response_id = r.id
            WHERE r.provider = ? AND p.outcome = 'success'
            ORDER BY r.requested_at DESC LIMIT 1
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
        capacity["capacity"] if capacity else None,
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
