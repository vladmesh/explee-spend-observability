import json
import sqlite3

import httpx
import pytest

from explee_test.observability import raw_poller


@pytest.mark.asyncio
async def test_cycle_survives_bad_responses(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/api/providers"):
            return httpx.Response(
                200,
                json=[
                    {"provider": "good", "endpoint": "/api/good/balance"},
                    {"provider": "html", "endpoint": "/api/html/balance"},
                    {"provider": "boom", "endpoint": "/api/boom/balance"},
                    {"provider": "bin", "endpoint": "/api/bin/balance"},
                ],
            )
        if "good" in path:
            return httpx.Response(200, json={"balance": 1.5}, headers={"date": "x"})
        if "html" in path:
            return httpx.Response(502, text="<html>bad gateway</html>")
        if "bin" in path:
            return httpx.Response(200, content=b"\xff\xfe\x00")
        raise httpx.ConnectError("nope")

    sink = raw_poller.Sink(tmp_path / "raw.sqlite3", tmp_path / "raw.jsonl")
    monkeypatch.setattr(raw_poller, "SERVER_ERROR_DELAY_SECONDS", 0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rows = await raw_poller.run_cycle(client, "https://x.test/api", sink)

    by = {r["provider"]: r for r in rows if r["attempt"] == 1}
    # The 502 is retried once and fails again: one retry, never a loop.
    retries = [r for r in rows if r["attempt"] > 1]
    assert [r["provider"] for r in retries] == ["html"]
    assert retries[0]["status_code"] == 502
    assert by["_catalog"]["status_code"] == 200
    assert by["good"]["body_is_json"] == 1 and by["good"]["server_date"] == "x"
    assert by["html"]["status_code"] == 502 and by["html"]["body_is_json"] == 0
    assert by["boom"]["error_type"] == "ConnectError"
    assert by["bin"]["body_b64"] is not None

    with sqlite3.connect(tmp_path / "raw.sqlite3") as c:
        assert c.execute("SELECT count(*) FROM raw_responses").fetchone()[0] == 6
    assert len((tmp_path / "raw.jsonl").read_text().splitlines()) == 6
    first = json.loads((tmp_path / "raw.jsonl").read_text().splitlines()[0])
    assert first["provider"] == "_catalog"
