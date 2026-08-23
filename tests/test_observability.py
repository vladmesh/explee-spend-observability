import json
import sqlite3
import statistics
from datetime import UTC, datetime, timedelta

import pytest

from explee_test.observability.adapters import SemanticError, normalize_payload
from explee_test.observability.dashboard import build_overview, build_provider_detail
from explee_test.observability.normalizer import process_all_pending
from explee_test.observability.store import initialise_database


@pytest.mark.parametrize(
    ("provider", "payload", "expected"),
    [
        ("openai", {"balance": 50.25, "currency": "USD"}, [("provider_balance", 50.25)]),
        (
            "evomi",
            {"ok": True, "data": {"wallet": {"amount": 12.5, "ccy": "usd"}}},
            [("provider_balance", 12.5)],
        ),
        (
            "resend",
            {"remaining": 900, "package": 1000, "refresh": "2026-09-01"},
            [("provider_credits_remaining", 900)],
        ),
        (
            "anthropic",
            {"object": "cost_report", "amount_cents": 1234, "window": "trailing_24h"},
            [("provider_spend", 12.34)],
        ),
        ("tremendous", {"gbp": 123.4}, [("provider_balance", 123.4)]),
        ("vastai", {"credit": -5.5, "unit": "usd"}, [("provider_credit", -5.5)]),
        (
            "meta_ads",
            {"spend_usd_30d": 300, "spend_usd_24h": 10},
            [("provider_spend", 10), ("provider_spend", 300)],
        ),
    ],
)
def test_provider_payload_shapes(provider, payload, expected) -> None:
    samples = normalize_payload(provider, payload)

    assert [(sample.metric_name, sample.value) for sample in samples] == expected


def test_package_adapter_rejects_impossible_remaining() -> None:
    with pytest.raises(SemanticError):
        normalize_payload(
            "resend",
            {"remaining": 1100, "package": 1000, "refresh": "2026-09-01"},
        )


def _raw_row(provider: str, at: str, status: int, body: str) -> tuple:
    return (
        "cycle",
        at,
        provider,
        f"https://example.test/{provider}",
        at,
        at,
        125.0,
        "HTTP/1.1",
        status,
        "OK" if status == 200 else "Service Unavailable",
        "[]",
        None,
        "application/json",
        len(body),
        body,
        None,
        1,
        None,
        None,
    )


def test_normalization_outcomes_and_dashboard_projection(tmp_path) -> None:
    path = tmp_path / "raw.sqlite3"
    initialise_database(path)
    at = "2026-08-23T00:00:00+00:00"
    rows = [
        _raw_row("openai", at, 200, json.dumps({"balance": 10, "currency": "USD"})),
        _raw_row("openai", "2026-08-23T00:00:30+00:00", 200, "{}"),
        _raw_row("openai", "2026-08-23T00:01:00+00:00", 503, '{"error":"down"}'),
    ]
    with sqlite3.connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO raw_responses (
                cycle_id, cycle_started_at, provider, url, requested_at, responded_at,
                latency_ms, http_version, status_code, reason_phrase, headers_json,
                server_date, content_type, body_bytes, body_text, body_b64,
                body_is_json, error_type, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    assert process_all_pending(path) == 3
    assert process_all_pending(path) == 0
    with sqlite3.connect(path) as connection:
        outcomes = connection.execute(
            "SELECT outcome FROM processing_results ORDER BY raw_response_id"
        ).fetchall()
        observation = connection.execute(
            "SELECT raw_response_id, metric_name, value FROM observations"
        ).fetchone()
    assert outcomes == [("success",), ("empty_payload",), ("http_error",)]
    assert observation == (1, "provider_balance", 10.0)

    overview = build_overview(path, 1, now=datetime(2026, 8, 23, 0, 2, tzinfo=UTC))
    openai = next(item for item in overview["providers"] if item["provider"] == "openai")
    assert openai["value"] == 10
    assert openai["valid_percent"] == 33.3
    assert openai["last_attempt"]["outcome"] == "http_error"
    assert openai["failure_streak"]["count"] == 2

    detail = build_provider_detail(path, "openai", 24)
    assert detail["latest_raw"]["raw_response_id"] == 1
    assert detail["series"][0]["points"] == [[at, 10.0, 1]]


def _spend_row(provider: str, at: str, body: str) -> tuple:
    return _raw_row(provider, at, 200, body)


def test_spend_report_rate_is_outflow_not_window_slope(tmp_path) -> None:
    """A trailing-window spend total burns value/window per hour, whatever its slope."""

    path = tmp_path / "raw.sqlite3"
    initialise_database(path)
    # Flat window level plus one glitch sample of the kind meta_ads emits at
    # recomputation boundaries: neither may move the reported burn rate.
    levels = [720.0, 720.0, 620.0, 720.0, 720.0, 720.0]
    rows = [
        _spend_row(
            "meta_ads",
            f"2026-08-23T00:{index:02d}:00+00:00",
            json.dumps({"spend_usd_24h": level, "spend_usd_30d": level * 30}),
        )
        for index, level in enumerate(levels)
    ]
    with sqlite3.connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO raw_responses (
                cycle_id, cycle_started_at, provider, url, requested_at, responded_at,
                latency_ms, http_version, status_code, reason_phrase, headers_json,
                server_date, content_type, body_bytes, body_text, body_b64,
                body_is_json, error_type, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    process_all_pending(path)

    overview = build_overview(path, 1, now=datetime(2026, 8, 23, 0, 6, tzinfo=UTC))
    meta = next(item for item in overview["providers"] if item["provider"] == "meta_ads")

    assert meta["rate_kind"] == "spend"
    assert meta["spend_window_hours"] == 24
    assert meta["rate_per_hour"] == 30.0
    assert meta["forecast"] == {"kind": "projected_30d", "value": 21600.0}


def test_spend_report_forecast_replaces_runway() -> None:
    from explee_test.observability.adapters import PROVIDERS
    from explee_test.observability.dashboard import _forecast, _spend_per_hour, _window_hours

    assert _window_hours('{"window":"trailing_24h"}') == 24
    assert _window_hours('{"window":"trailing_30d"}') == 720
    assert _window_hours("{}") is None

    points = [("2026-08-23T00:00:00+00:00", 240.0)] * 5
    rate = _spend_per_hour(points, 24)
    assert rate == 10.0
    forecast = _forecast(
        PROVIDERS["anthropic"], 240.0, rate, None, datetime(2026, 8, 23, tzinfo=UTC)
    )
    assert forecast == {"kind": "projected_30d", "value": 7200.0}


def _points(values: list[float], step_seconds: int = 30) -> list[tuple[str, float]]:
    base = datetime(2026, 8, 23, tzinfo=UTC)
    return [
        ((base + timedelta(seconds=index * step_seconds)).isoformat(), value)
        for index, value in enumerate(values)
    ]


def test_rate_survives_a_series_quantised_to_the_poll_interval() -> None:
    """bounceban only moves on some polls; adjacent slopes were quantised to zero."""

    from explee_test.observability.dashboard import _rate_per_hour

    # -24 credits/hour delivered as one integer step every fifth 30s poll.
    values = [6800.0 - (index // 5) for index in range(121)]
    points = _points(values)

    adjacent = [
        (right - left) * 120 for left, right in zip(values, values[1:], strict=False)
    ]
    assert statistics.median(adjacent) == 0  # what the old estimator saw

    rate = _rate_per_hour(points)
    assert rate is not None
    assert -25 < rate < -23


def test_rate_ignores_an_isolated_top_up_and_a_single_glitch() -> None:
    from explee_test.observability.dashboard import _rate_per_hour

    values = [1000.0 - index * 0.5 for index in range(121)]
    values = [value + (400 if index >= 60 else 0) for index, value in enumerate(values)]
    values[90] = 0.0  # glitch sample of the meta_ads kind

    rate = _rate_per_hour(_points(values))
    assert rate is not None
    assert -62 < rate < -58  # -0.5 per 30s == -60/hour


def test_despike_removes_single_samples_but_keeps_a_real_step() -> None:
    from explee_test.observability.dashboard import _despike

    glitch = [value for _, value in _despike(_points([10.0, 10.0, 0.0, 10.0, 10.0]))]
    assert glitch == [10.0, 10.0, 10.0, 10.0, 10.0]

    step = [value for _, value in _despike(_points([10.0, 10.0, 50.0, 50.0, 50.0]))]
    assert step == [10.0, 10.0, 50.0, 50.0, 50.0]


def test_package_that_empties_before_refresh_never_projects_negative_credits() -> None:
    """scrapfly used to report -40 083 credits 'at refresh'; credits stop at zero."""

    from explee_test.observability.adapters import PROVIDERS
    from explee_test.observability.dashboard import _forecast, _risk_hours

    now = datetime(2026, 8, 23, tzinfo=UTC)
    empties = _forecast(PROVIDERS["scrapfly"], 37000.0, -350.0, "2026-09-01", now)
    assert empties["kind"] == "before_refresh"
    assert empties["hours"] == pytest.approx(105.7, abs=0.1)
    assert _risk_hours(empties) == empties["hours"]

    survives = _forecast(PROVIDERS["scrapfly"], 37000.0, -20.0, "2026-09-01", now)
    assert survives["kind"] == "refresh_first"
    assert survives["value"] >= 0
    assert _risk_hours(survives) is None


def test_postpaid_debt_is_reported_rather_than_hidden() -> None:
    from explee_test.observability.adapters import PROVIDERS
    from explee_test.observability.dashboard import _forecast, _risk_hours

    debt = _forecast(PROVIDERS["vastai"], -25.53, -1.0, None, datetime(2026, 8, 23, tzinfo=UTC))
    assert debt == {"kind": "in_debt", "value": -25.53}
    assert _risk_hours(debt) == 0.0

    runway = _forecast(PROVIDERS["vastai"], 20.0, -10.0, None, datetime(2026, 8, 23, tzinfo=UTC))
    assert runway == {"kind": "runway", "hours": 2.0}


def test_relative_rate_ranks_unlike_units() -> None:
    from explee_test.observability.dashboard import _relative_rate

    assert _relative_rate(-25.0, 6500.0) == pytest.approx(-0.385, abs=0.001)
    assert _relative_rate(-350.0, 37000.0) == pytest.approx(-0.946, abs=0.001)
    assert _relative_rate(None, 10.0) is None
    assert _relative_rate(-1.0, 0) is None


def test_explicit_window_narrows_every_panel(tmp_path) -> None:
    path = tmp_path / "raw.sqlite3"
    initialise_database(path)
    rows = [
        _raw_row("openai", f"2026-08-23T00:{minute:02d}:00+00:00", 200,
                 json.dumps({"balance": 100 - minute, "currency": "USD"}))
        for minute in range(10)
    ]
    with sqlite3.connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO raw_responses (
                cycle_id, cycle_started_at, provider, url, requested_at, responded_at,
                latency_ms, http_version, status_code, reason_phrase, headers_json,
                server_date, content_type, body_bytes, body_text, body_b64,
                body_is_json, error_type, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    process_all_pending(path)

    narrowed = build_overview(
        path,
        12,
        now=datetime(2026, 8, 23, 1, tzinfo=UTC),
        start="2026-08-23T00:02:00+00:00",
        end="2026-08-23T00:05:00+00:00",
    )
    assert narrowed["summary"]["attempts"] == 4
    assert narrowed["range_start"].startswith("2026-08-23T00:02")
    assert narrowed["range_end"].startswith("2026-08-23T00:05")

    detail = build_provider_detail(
        path, "openai", 12, start="2026-08-23T00:02:00+00:00", end="2026-08-23T00:05:00+00:00"
    )
    assert len(detail["attempts"]) == 4


def test_raw_response_endpoint_returns_the_evidence_for_one_point(tmp_path) -> None:
    from explee_test.observability.dashboard import get_raw_response

    path = tmp_path / "raw.sqlite3"
    initialise_database(path)
    body = json.dumps({"balance": 10, "currency": "USD"})
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO raw_responses (
                cycle_id, cycle_started_at, provider, url, requested_at, responded_at,
                latency_ms, http_version, status_code, reason_phrase, headers_json,
                server_date, content_type, body_bytes, body_text, body_b64,
                body_is_json, error_type, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _raw_row("openai", "2026-08-23T00:00:00+00:00", 200, body),
        )
    process_all_pending(path)

    evidence = get_raw_response(path, 1)
    assert evidence["payload"] == {"balance": 10, "currency": "USD"}
    assert evidence["outcome"] == "success"
    assert "headers_json" not in evidence
    with pytest.raises(KeyError):
        get_raw_response(path, 999)


def test_debt_is_counted_apart_from_providers_that_may_yet_run_out(tmp_path) -> None:
    """'1 of 15 reach zero soon' hid that the one had already gone past zero."""

    path = tmp_path / "raw.sqlite3"
    initialise_database(path)
    rows = []
    for minute in range(6):
        rows.append(
            _raw_row("vastai", f"2026-08-23T00:{minute:02d}:00+00:00", 200,
                     json.dumps({"credit": -5.0 - minute, "unit": "usd"}))
        )
        rows.append(
            _raw_row("openai", f"2026-08-23T00:{minute:02d}:00+00:00", 200,
                     json.dumps({"balance": 500 - minute, "currency": "USD"}))
        )
    with sqlite3.connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO raw_responses (
                cycle_id, cycle_started_at, provider, url, requested_at, responded_at,
                latency_ms, http_version, status_code, reason_phrase, headers_json,
                server_date, content_type, body_bytes, body_text, body_b64,
                body_is_json, error_type, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    process_all_pending(path)

    summary = build_overview(path, 1, now=datetime(2026, 8, 23, 0, 6, tzinfo=UTC))["summary"]

    assert summary["in_debt"] == 1
    assert summary["in_debt_providers"] == ["vastai"]
    # openai is burning fast enough to be at risk, and must not be hidden behind
    # the debt count, nor counted twice.
    assert "vastai" not in summary["at_risk_providers"]
    assert summary["at_risk"] == len(summary["at_risk_providers"])


def _load(path, provider, values, payload):
    rows = [
        _raw_row(provider, f"2026-08-23T00:{minute:02d}:00+00:00", 200, json.dumps(payload(value)))
        for minute, value in enumerate(values)
    ]
    with sqlite3.connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO raw_responses (
                cycle_id, cycle_started_at, provider, url, requested_at, responded_at,
                latency_ms, http_version, status_code, reason_phrase, headers_json,
                server_date, content_type, body_bytes, body_text, body_b64,
                body_is_json, error_type, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    process_all_pending(path)


def _balance(value):
    return {"balance": value, "currency": "USD"}


def test_spend_over_a_window_is_outflow_not_net_change(tmp_path) -> None:
    """A balance that grew can still have spent money; net change hides that."""

    path = tmp_path / "raw.sqlite3"
    initialise_database(path)
    # Falls steadily, receives a top-up that more than covers it, keeps falling.
    _load(path, "openai", [500, 499, 498, 497, 496, 796, 796, 795, 794, 793], _balance)

    overview = build_overview(path, 1, now=datetime(2026, 8, 23, 0, 10, tzinfo=UTC))
    openai = next(item for item in overview["providers"] if item["provider"] == "openai")

    assert openai["window_net"] > 290  # the balance ended far higher than it started
    assert openai["window_spend"] > 0  # and money was spent all the same
    assert overview["summary"]["window_spend_usd"] == openai["window_spend"]


def test_a_monotone_window_spends_exactly_what_it_lost(tmp_path) -> None:
    path = tmp_path / "raw.sqlite3"
    initialise_database(path)
    _load(path, "openai", [500 - minute * 10 for minute in range(10)], _balance)

    overview = build_overview(path, 1, now=datetime(2026, 8, 23, 0, 10, tzinfo=UTC))
    openai = next(item for item in overview["providers"] if item["provider"] == "openai")

    assert openai["window_spend"] == 90.0
    assert openai["window_net"] == -90.0


def test_the_rate_window_is_reported_so_a_reader_can_see_what_it_covers(tmp_path) -> None:
    path = tmp_path / "raw.sqlite3"
    initialise_database(path)
    _load(path, "openai", [100 - minute for minute in range(10)], _balance)

    overview = build_overview(path, 12, now=datetime(2026, 8, 23, 0, 10, tzinfo=UTC))

    # Nine minutes of samples cannot claim a twelve hour window.
    assert overview["summary"]["rate_window_minutes"] == 9.0
