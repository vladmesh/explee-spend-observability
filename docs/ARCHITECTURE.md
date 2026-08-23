# Architecture

## Goal

Convert heterogeneous, unreliable current-value provider APIs into an explainable live view of spend, runway, freshness, and situations that deserve human attention.

## Data flow

```text
provider catalog
      |
parallel bounded poller -> raw response envelope -> provider adapter
      |                                             |
      +---------------- SQLite time series <-------+
                                |
                   derived signals and alert rules
                        |                 |
                  dashboard API      alerts.jsonl
```

Raw responses are retained before normalisation. This makes parser corrections auditable and lets the same captured sequence drive deterministic tests.

## Boundaries

- **Catalog client:** discovers provider identifiers and advertised payment models.
- **Provider adapters:** translate response-specific fields into a canonical observation without discarding raw data.
- **Store:** persists observations, errors, emitted alerts, and deduplication keys in SQLite/WAL.
- **Signal layer:** calculates freshness, consumption velocity, expected exhaustion, quota pacing, and sustained anomalies.
- **Policy layer:** decides when a human should look. It does not confuse normal top-ups with incidents.
- **Presentation:** exposes a read-only dashboard and health endpoint. Polling is not coupled to browser traffic.

## Initial alert hypotheses

These are starting hypotheses, not frozen thresholds:

1. A prepaid balance deserves attention when projected runway crosses a time threshold and consumption is sustained.
2. A monthly package deserves attention when remaining quota is behind the time-weighted budget for its refresh date.
3. A trailing spend report deserves attention on a statistically meaningful rate change, not merely a large absolute total.
4. A postpaid balance requires model-specific semantics; a negative value alone is not automatically an incident.
5. Missing or invalid data becomes its own alert only after sustained staleness, with recovery recorded separately.

Every emitted alert must carry enough evidence to reproduce the decision and must have a deduplication/cooldown strategy.

## Deployment

The same application image runs as two small containers: a background poller/normaliser and the
FastAPI read surface. They share a bind-mounted SQLite/WAL database and JSONL capture directory on a
durable volume. Keeping the processes separate prevents a dashboard restart from interrupting
collection, while avoiding a queue or external database. Caddy exposes only the FastAPI service on
the provider-assigned hostname; the poller has no published port.
