# Provider spend observability

Live view of what fifteen provider billing APIs are costing, what is about to run out, and whether
the numbers on screen can be trusted. Built for the [Explee AI-native Developer test](https://jobs.explee.com/ai-native-developer/test), task 1.

**Live dashboard: <https://5vei.l.time4vps.cloud/>** — opens without login; the deployment runs this
repository's `main` (`/opt/services/explee` on the host is a checkout of it).

## Submission

What the task asks for and where each piece is:

| deliverable | where |
| --- | --- |
| code | this repository |
| dashboard | <https://5vei.l.time4vps.cloud/> |
| `alerts.jsonl` | written by the collector on the host to `deploy/data/alerts.jsonl`; a copy is attached to the submission. One JSON line per transition with `ts` (ISO-8601, offset), `text`, `event` (`opened`/`resolved`), `rule`, `severity`, `provider`, `dedupe_key` and `evidence`; only `page` and `today` reach the file |
| `TRACE.md` | the agent conversations are kept in the private working repository and attached to the submission |

The collector has run continuously since 2026-08-22T13:51Z; the journal in its current form starts
at 2026-08-23T12:58Z, when the alert policy reached its final shape, and everything written before
that is kept on the host under `deploy/data/archive/`.

## What it does

A collector polls every provider every 30 seconds and stores the response verbatim before
interpreting it. A separate pass turns stored responses into canonical observations, each one keeping
the identifier of the response it came from, so any number on the dashboard can be traced back to the
bytes a provider actually returned. A policy layer reads the same projection the dashboard draws and
decides which conditions deserve a human.

The design assumption throughout is that provider APIs misbehave: they throttle, they return `{}` with
HTTP 200, they emit single-sample glitches, and they go down for minutes at a time. Every one of those
was measured on live traffic before being handled, and the measurements are written down next to the
code that acts on them.

## What is worth looking at

- **[`docs/TASK1_DASHBOARD.md`](docs/TASK1_DASHBOARD.md)** — the pipeline, the outcome contract, how
  rates and projections are computed, the alert policy, and the screen model.
- **[`observability/alerts.py`](src/explee_test/observability/alerts.py)** — nine rules, each with
  the reason its threshold is what it is.
- **[`observability/events.py`](src/explee_test/observability/events.py)** — top-ups, package resets,
  drawdowns, spend spikes, source glitches and outages, derived from stored observations.
- **[`Opus_review.md`](Opus_review.md)** and **[`Fable_review.md`](Fable_review.md)** — two ranked
  reviews of this codebase with what was wrong, what was fixed, and what is still open. They include
  the measurements behind several of the decisions.
- **[`notes/polling/`](notes/polling)** — observations taken while the collector ran.

Three findings shaped the implementation more than anything else:

**429 is a counter, not a rate.** Two providers throttle roughly a quarter of requests. Forty
requests back to back and forty paced one per second both give exactly every fourth request a 429; a
request to a different endpoint advances the same counter; a 35-second pause does not reset it.
Polling slower changes nothing, so the collector honours the stated `Retry-After` once per cycle
instead of backing off.

**Adjacent-sample slopes are quantised to zero.** A provider that changes its value less often than
we poll made the median adjacent slope exactly zero while it was burning 25 credits per hour, and the
dashboard reported no spend and no runway. Rates are now measured across a fixed ten-minute lag.

**A spend report is not a balance.** `spend_usd_24h` is a cumulative total over a moving window, so
its slope measures how the window moves rather than how much is being spent. The burn rate is the
window level divided by the window length; cross-checked against the source's own 30-day field, the
two agree to 0.05%.

## Running it

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
uv sync --all-groups

# collect
uv run python -m explee_test.observability.raw_poller --interval 30

# read
uv run uvicorn explee_test.main:app --reload
```

Open <http://127.0.0.1:8000/>. Checks:

```bash
uv run ruff check .
uv run pytest
```

## Deploying

`deploy/docker-compose.yml` runs the same image as two containers — a collector and a read surface —
sharing a bind-mounted SQLite database and the alert journal. Keeping them apart means a dashboard
restart never interrupts collection, and it gives the collector a watchdog that can report its death,
which it cannot do itself. `deploy/Caddyfile.snippet` is the host block that publishes the read
surface; the collector has no published port.

## Public surface

- `/` — the dashboard;
- `/health` — process health;
- `/api/overview` — the window projection, by preset hours or an explicit `start`/`end`;
- `/api/providers/{provider}` — canonical series, classified outcomes and detected events;
- `/api/raw/{id}` — the stored response behind one plotted point;
- `/api/alerts` — what is open now and what came and went.

Headers, URLs, transport error text and arbitrary queries are not exposed.

## Layout

```text
src/explee_test/
  main.py                 HTTP surface, security headers, collector watchdog
  settings.py             environment configuration
  observability/
    raw_poller.py         capture, retries, cycle bookkeeping
    normalizer.py         outcome classification, idempotent backfill
    adapters.py           per-provider payload translation
    dashboard.py          read models: rates, risk, events, quality
    events.py             top-ups, resets, glitches, outages
    alerts.py             alert policy and reconciliation
    store.py              SQLite schema and migrations
  web/                    the dashboard page, its script and its vendored chart library
docs/                     architecture, dashboard and alert reference, trace policy
deploy/                   compose stack and the Caddy host block
notes/polling/            observations taken during collection
```

## Data

Collected telemetry is not committed. Runtime data lives in the ignored `data/` directory, and the
alert journal is written to the path named by `EXPLEE_ALERTS_PATH`.
