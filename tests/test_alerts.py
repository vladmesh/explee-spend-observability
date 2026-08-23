from datetime import UTC, datetime, timedelta

from explee_test.observability.alerts import (
    Alert,
    collector_alerts,
    evaluate,
    open_alerts,
    open_keys,
    reconcile,
)
from explee_test.observability.store import initialise_database

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _provider(**overrides):
    base = {
        "provider": "openai",
        "name": "OpenAI",
        "pay_model": "prepaid_balance",
        "unit": "usd",
        "value": 500.0,
        "rate_per_hour": -5.0,
        "risk_hours": 100.0,
        "cycles": 240,
        "missing_cycles": 0,
        "outcomes": {"success": 240},
        "forecast": {"kind": "runway", "hours": 100.0},
        "observed_at": NOW.isoformat(),
        "last_attempt": {"outcome": "success"},
    }
    return {**base, **overrides}


def _overview(*providers):
    return {"providers": list(providers)}


def test_a_healthy_provider_raises_nothing() -> None:
    assert evaluate(_overview(_provider()), set()) == []


def test_only_the_most_urgent_runway_threshold_is_raised() -> None:
    """1.4 hours left does not need three alerts saying so."""

    alerts = evaluate(_overview(_provider(risk_hours=1.4)), set())

    assert [alert.rule for alert in alerts] == ["runway_5h"]
    alerts = evaluate(_overview(_provider(risk_hours=0.5)), set())
    assert [(alert.rule, alert.severity) for alert in alerts] == [("runway_1h", "page")]


def test_a_runway_alert_does_not_flap_around_its_threshold() -> None:
    open_now = {"runway_5h:openai"}
    # Still open at 6 hours, because closing at 5.1 and reopening at 4.9 is noise.
    assert [a.rule for a in evaluate(_overview(_provider(risk_hours=6.0)), open_now)] == [
        "runway_5h"
    ]
    # Genuinely recovered: the 5h alert goes, and only the calmer one remains.
    assert [a.rule for a in evaluate(_overview(_provider(risk_hours=9.0)), open_now)] == [
        "runway_24h"
    ]


def test_a_fresh_restart_does_not_predict_from_two_samples() -> None:
    assert evaluate(_overview(_provider(risk_hours=0.5, cycles=3)), set()) == []


def test_crossing_zero_alerts_once_and_replaces_the_runway_alert() -> None:
    debtor = _provider(provider="vastai", pay_model="postpaid", value=-7.5)
    alerts = evaluate(_overview(debtor), set())

    assert [(alert.rule, alert.severity) for alert in alerts] == [("exhausted", "page")]
    assert "accruing debt" in alerts[0].text
    # Still one alert, still the same key: debt does not re-fire every cycle.
    deeper = _provider(provider="vastai", pay_model="postpaid", value=-40.0)
    again = evaluate(_overview(deeper), {alerts[0].key})
    assert [alert.key for alert in again] == [alerts[0].key]


def test_a_blind_provider_is_reported_as_blind_and_never_predicted_through() -> None:
    alerts = evaluate(
        _overview(
            _provider(missing_cycles=12, risk_hours=0.5),
            _provider(provider="evomi", name="Smartproxy"),
            _provider(provider="resend", name="Resend"),
        ),
        set(),
    )

    assert [alert.rule for alert in alerts] == ["provider_silent"]
    assert alerts[0].severity == "today"
    assert alerts[0].evidence["missing_cycles"] == 12


def test_everything_down_at_once_is_one_incident() -> None:
    alerts = evaluate(
        _overview(
            _provider(missing_cycles=8),
            _provider(provider="evomi", name="Smartproxy", missing_cycles=8),
            _provider(provider="resend", name="Resend", missing_cycles=8),
        ),
        set(),
    )

    assert [alert.rule for alert in alerts] == ["api_down"]


def test_an_unreadable_payload_is_a_contract_alert() -> None:
    alerts = evaluate(_overview(_provider(outcomes={"normalization": 4})), set())

    assert [alert.rule for alert in alerts] == ["schema_drift"]
    assert alerts[0].evidence["failed_attempts"] == 4


def test_collector_is_watched_from_outside_and_quiet_before_it_starts(tmp_path) -> None:
    import sqlite3

    path = tmp_path / "raw.sqlite3"
    initialise_database(path)
    assert collector_alerts(path, NOW) == []  # never started is not "died"

    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO raw_responses (cycle_id, cycle_started_at, provider, url, requested_at)"
            " VALUES ('c', ?, 'openai', 'u', ?)",
            (NOW.isoformat(), NOW.isoformat()),
        )

    assert collector_alerts(path, NOW + timedelta(seconds=60)) == []
    late = collector_alerts(path, NOW + timedelta(minutes=9))
    assert [(alert.rule, alert.severity) for alert in late] == [("collector_dead", "page")]


def test_an_open_alert_is_written_once_and_resolved_once(tmp_path) -> None:
    path = tmp_path / "raw.sqlite3"
    journal = tmp_path / "alerts.jsonl"
    initialise_database(path)
    alert = Alert(key="runway_1h:openai", rule="runway_1h", severity="page", text="x")

    assert reconcile(path, journal, [alert], NOW) == {"opened": 1, "resolved": 0, "open": 1}
    assert reconcile(path, journal, [alert], NOW) == {"opened": 0, "resolved": 0, "open": 1}
    assert open_keys(path) == {"runway_1h:openai"}
    assert reconcile(path, journal, [], NOW) == {"opened": 0, "resolved": 1, "open": 0}
    assert open_alerts(path) == []
    # Reopening after a real recovery is a new episode, not a duplicate.
    assert reconcile(path, journal, [alert], NOW)["opened"] == 1

    lines = journal.read_text().splitlines()
    assert [line.count('"event": "opened"') for line in lines] == [1, 0, 1]
    assert lines[1].count('"event": "resolved"') == 1


def test_an_alert_can_reopen_on_a_database_written_by_the_first_schema(tmp_path) -> None:
    """The original schema made dedupe_key unique table-wide, which forbade reopening."""

    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE alerts (
                id INTEGER PRIMARY KEY,
                emitted_at TEXT NOT NULL,
                provider TEXT,
                rule TEXT NOT NULL,
                text TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                dedupe_key TEXT NOT NULL UNIQUE
            )
            """
        )
    initialise_database(path)
    journal = tmp_path / "alerts.jsonl"
    alert = Alert(key="runway_1h:openai", rule="runway_1h", severity="page", text="x")

    assert reconcile(path, journal, [alert], NOW)["opened"] == 1
    assert reconcile(path, journal, [], NOW)["resolved"] == 1
    assert reconcile(path, journal, [alert], NOW)["opened"] == 1


def test_a_writer_never_resolves_an_alert_it_cannot_judge(tmp_path) -> None:
    """The collector proves it is alive by writing; it still may not judge itself."""

    from explee_test.observability.alerts import COLLECTOR_RULES, PROVIDER_RULES

    path = tmp_path / "raw.sqlite3"
    journal = tmp_path / "alerts.jsonl"
    initialise_database(path)
    watchdog = Alert(key="collector_dead", rule="collector_dead", severity="page", text="dead")
    runway = Alert(key="runway_1h:openai", rule="runway_1h", severity="page", text="soon")
    reconcile(path, journal, [watchdog], NOW, resolves=COLLECTOR_RULES)
    reconcile(path, journal, [runway], NOW, resolves=PROVIDER_RULES)

    assert open_keys(path) == {"collector_dead", "runway_1h:openai"}

    # The provider pass no longer sees the runway condition; the watchdog alert
    # is none of its business and must survive.
    reconcile(path, journal, [], NOW, resolves=PROVIDER_RULES)
    assert open_keys(path) == {"collector_dead"}


def test_a_condition_that_stops_being_true_closes_itself(tmp_path) -> None:
    """Nobody acknowledges an alert: a top-up or a recovery closes it."""

    from explee_test.observability.alerts import PROVIDER_RULES, recent_alerts

    path = tmp_path / "raw.sqlite3"
    journal = tmp_path / "alerts.jsonl"
    initialise_database(path)
    empty = _provider(provider="vastai", pay_model="postpaid", value=-7.5)
    topped_up = _provider(provider="vastai", pay_model="postpaid", value=120.0)

    reconcile(path, journal, evaluate(_overview(empty), set()), NOW, resolves=PROVIDER_RULES)
    assert [alert["rule"] for alert in open_alerts(path)] == ["exhausted"]

    # The balance came back; the condition is no longer true, so the alert closes.
    reconcile(path, journal, evaluate(_overview(topped_up), set()), NOW, resolves=PROVIDER_RULES)
    assert open_alerts(path) == []

    closed = [alert for alert in recent_alerts(path, 1) if alert["resolved_at"]]
    assert [alert["rule"] for alert in closed] == ["exhausted"]


def test_every_journal_line_carries_the_required_ts_and_text_keys(tmp_path) -> None:
    """The task grades `ts` (ISO-8601 with an offset) and `text` on every line."""

    import json

    path = tmp_path / "raw.sqlite3"
    journal = tmp_path / "alerts.jsonl"
    initialise_database(path)
    alert = Alert(key="runway_1h:openai", rule="runway_1h", severity="page", text="x")
    reconcile(path, journal, [alert], NOW)
    reconcile(path, journal, [], NOW)

    for line in journal.read_text().splitlines():
        payload = json.loads(line)
        assert payload["text"]
        assert payload["ts"] == payload["at"]
        assert datetime.fromisoformat(payload["ts"]).utcoffset() is not None
