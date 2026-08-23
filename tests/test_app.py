from fastapi.testclient import TestClient

from explee_test.main import app
from explee_test.settings import get_settings


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("EXPLEE_DATABASE_PATH", str(tmp_path / "monitor.sqlite3"))
    get_settings.cache_clear()
    return TestClient(app)


def test_health(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_dashboard_is_available(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Provider spend" in response.text


def test_empty_dashboard_api_is_available(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/api/overview?hours=6")

    assert response.status_code == 200
    assert response.json()["summary"] == {
        "providers": 15,
        "fresh": 0,
        "degraded": 0,
        "attempts": 0,
        "throttled_recovered": 0,
        "valid_percent": None,
        "p95_latency_ms": None,
        "usd_burn_per_hour": 0,
        "usd_projected_30d": 0,
        "usd_sources": 0,
        "rate_window_minutes": None,
        "window_spend_usd": 0,
        "window_covered_hours": 0.0,
        "window_spend_per_hour": None,
        "at_risk": 0,
        "at_risk_providers": [],
        "in_debt": 0,
        "in_debt_providers": [],
        "at_risk_hours": 48.0,
        "events": 0,
    }


def test_page_forbids_third_party_script_and_caches_vendored_files(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        page = client.get("/")
        vendored = client.get("/static/vendor/echarts-5.6.0.min.js")

    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert "cdn.jsdelivr.net" not in page.text
    assert vendored.status_code == 200
    assert vendored.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_the_page_and_its_scripts_are_revalidated_on_every_visit(tmp_path, monkeypatch) -> None:
    """Without a policy a browser may keep running a previous deployment's code."""

    with _client(tmp_path, monkeypatch) as client:
        page = client.get("/")
        script = client.get("/static/app.js")
        vendored = client.get("/static/vendor/echarts-5.6.0.min.js")

    assert page.headers["cache-control"] == "no-cache"
    assert script.headers["cache-control"] == "no-cache"
    assert "immutable" in vendored.headers["cache-control"]
