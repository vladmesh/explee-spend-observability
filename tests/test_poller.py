import asyncio
import json

from explee_test.observability.raw_poller import (
    failed_providers,
    retry_delay,
    retry_recoverable,
)


def _row(provider="findymail", status=200, body='{"balance": 1}', headers=None):
    return {
        "provider": provider,
        "status_code": status,
        "body_text": body,
        "headers_json": json.dumps(headers or []),
        "error_type": None,
    }


def test_a_stated_delay_is_honoured_including_by_not_retrying() -> None:
    assert retry_delay(_row(status=429, headers=[["retry-after", "5"]])) == 5.0
    # 504 asks for two minutes: honouring that means not asking again this cycle.
    assert retry_delay(_row(status=504, headers=[["Retry-After", "120"]])) is None
    http_date = [["retry-after", "Wed, 21 Oct 2026 07:28:00 GMT"]]
    assert retry_delay(_row(status=503, headers=http_date)) is None


def test_transient_shapes_are_retried_and_healthy_ones_are_not() -> None:
    assert retry_delay(_row(status=500)) == 2.0
    assert retry_delay(_row(status=200, body="{}")) == 1.0
    assert retry_delay(_row(status=200, body='{"balance": 10}')) is None
    assert retry_delay(_row(status=404)) is None
    assert retry_delay(_row(status=None)) is None


def test_a_provider_that_is_already_down_is_not_retried() -> None:
    """Retrying a blip helps; doubling the rate against an outage only adds load."""

    calls = []

    class _Client:
        async def get(self, url):
            calls.append(url)
            raise AssertionError("no request should be made")

    rows = [_row(provider="evomi", status=500)]
    endpoints = {"evomi": "/api/evomi/balance"}
    retried = asyncio.run(
        retry_recoverable(
            _Client(), rows, endpoints, "https://x", "cycle", "at", failing={"evomi"}
        )
    )

    assert retried == []
    assert calls == []


def test_failed_providers_counts_a_cycle_not_a_request() -> None:
    rows = [
        _row(provider="findymail", status=429, body='{"error":"rate limited"}'),
        _row(provider="findymail", status=200, body='{"remaining": 5}'),
        _row(provider="evomi", status=500, body="upstream"),
        _row(provider="vastai", status=200, body="{}"),
    ]

    assert failed_providers(rows) == {"evomi", "vastai"}
