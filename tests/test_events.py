import pytest

from explee_test.observability.events import detect_outages, detect_series_events


def _points(values, start=0, step=30):
    stamps = [start + index * step for index in range(len(values))]
    return [
        (f"2026-08-23T00:{second // 60:02d}:{second % 60:02d}+00:00", value, 100 + index)
        for index, (second, value) in enumerate(zip(stamps, values, strict=True))
    ]


def test_single_sample_dip_is_a_glitch_not_a_step() -> None:
    """meta_ads drops one sample at each recomputation boundary and returns."""

    values = [1000.0 - index * 0.1 for index in range(20)]
    values[10] = 900.0
    events = detect_series_events("meta_ads", "spend_report", "usd", _points(values))

    glitches = [event for event in events if event.kind == "glitch"]
    assert len(glitches) == 1
    assert glitches[0].raw_response_id == 110
    assert glitches[0].magnitude == pytest.approx(-99.0, abs=1.0)


def test_persistent_rise_is_a_top_up() -> None:
    values = [200.0 - index for index in range(10)] + [280.0 - index for index in range(10)]
    events = detect_series_events("openrouter", "prepaid_balance", "usd", _points(values))

    top_ups = [event for event in events if event.kind == "top_up"]
    assert len(top_ups) == 1
    assert top_ups[0].magnitude > 80
    assert not [event for event in events if event.kind == "glitch"]


def test_refill_close_to_capacity_is_a_package_reset() -> None:
    values = [1000.0 - index * 10 for index in range(10)]
    values += [49000.0 - index * 10 for index in range(10)]
    events = detect_series_events(
        "scrapfly", "credits_package", "credits", _points(values), capacity=50000.0
    )

    assert [event.kind for event in events if event.kind != "glitch"] == ["package_reset"]


def test_persistent_fall_is_a_drawdown() -> None:
    values = [500.0] * 10 + [300.0] * 10
    events = detect_series_events("openai", "prepaid_balance", "usd", _points(values))

    assert [event.kind for event in events] == ["drawdown"]


def test_spend_window_above_its_baseline_is_a_spike() -> None:
    values = [300.0] * 10 + [900.0] * 6 + [300.0] * 10
    events = detect_series_events("meta_ads", "spend_report", "usd", _points(values))

    spikes = [event for event in events if event.kind == "spend_spike"]
    assert len(spikes) == 1
    assert spikes[0].ended_at is not None
    assert spikes[0].magnitude == 900.0


def test_a_steady_series_produces_no_events() -> None:
    values = [500.0 - index * 0.2 for index in range(30)]
    assert detect_series_events("openai", "prepaid_balance", "usd", _points(values)) == []


def test_consecutive_failures_group_into_one_outage() -> None:
    attempts = [
        ("2026-08-23T00:00:00+00:00", "success", "success"),
        ("2026-08-23T00:00:30+00:00", "http_error", "http_5xx"),
        ("2026-08-23T00:01:00+00:00", "http_error", "http_5xx"),
        ("2026-08-23T00:01:30+00:00", "http_error", "http_other"),
        ("2026-08-23T00:02:00+00:00", "success", "success"),
        ("2026-08-23T00:02:30+00:00", "http_error", "http_5xx"),
    ]
    events = detect_outages("twocaptcha", attempts)

    assert len(events) == 1
    assert events[0].magnitude == 3.0
    assert events[0].at.endswith("00:00:30+00:00")
    assert events[0].ended_at.endswith("00:01:30+00:00")
    assert "http_5xx" in events[0].detail


def test_throttling_is_never_an_outage() -> None:
    """findymail answers 'retry in five seconds' on a quarter of all requests."""

    attempts = [
        ("2026-08-23T00:00:00+00:00", "throttled", "rate_limited"),
        ("2026-08-23T00:00:05+00:00", "success", "success"),
        ("2026-08-23T00:00:30+00:00", "throttled", "rate_limited"),
        ("2026-08-23T00:00:35+00:00", "throttled", "rate_limited"),
        ("2026-08-23T00:01:00+00:00", "throttled", "rate_limited"),
    ]
    assert detect_outages("findymail", attempts) == []


def test_an_outage_needs_three_polls_without_data_not_three_requests() -> None:
    """A cycle whose retry succeeded delivered its data and is not an incident."""

    cycles = [
        ("2026-08-23T00:00:00+00:00", "success", None),
        ("2026-08-23T00:00:30+00:00", "success", "http_5xx"),  # failed, then retried
        ("2026-08-23T00:01:00+00:00", "no_data", "http_5xx"),
        ("2026-08-23T00:01:30+00:00", "no_data", "http_5xx"),
        ("2026-08-23T00:02:00+00:00", "success", None),
    ]
    assert detect_outages("evomi", cycles) == []

    cycles[4] = ("2026-08-23T00:02:00+00:00", "no_data", "http_5xx")
    events = detect_outages("evomi", cycles)
    assert len(events) == 1
    assert events[0].magnitude == 3.0
    assert "without data" in events[0].detail
