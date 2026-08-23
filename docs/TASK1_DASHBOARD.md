# Task 1 dashboard and metric projection

## Pipeline

Every poll attempt is appended to `raw_responses` before interpretation. A normalisation pass then
creates exactly one `processing_results` row linked by `raw_response_id`. Successful passes create
one or more canonical `observations`; unsuccessful passes retain a bounded error classification and
never invent a business value.

```text
HTTP attempt -> raw_responses -> outcome classification -> provider adapter
                                      |                       |
                                      | failure               +-> observations
                                      +-> processing_results          |
                                                              dashboard projection
```

The normaliser is idempotent because `processing_results.raw_response_id` is unique. It can backfill
stored responses with `python -m explee_test.observability.normalizer --db <path>` and is also called
after each live polling cycle. Adapter and observation rows carry an `adapter_version`; raw bodies
remain unchanged when parsing logic changes.

## Outcome contract

One attempt has one primary outcome:

- `success`: at least one canonical observation was written;
- `transport_error`: no usable HTTP response;
- `throttled`: HTTP 429, a stated delay rather than a broken provider;
- `http_error`: any other non-200 response, without also reporting a parse error for its body;
- `empty_payload`: HTTP 200 with `{}`;
- `invalid_json`: HTTP 200 whose body cannot be decoded as JSON;
- `schema_error`: JSON does not match the provider adapter;
- `semantic_error`: shape is recognised but violates its contract.

## Transient failures and retries

Three shapes in this feed are recoverable by asking once more, and all three were measured before
anything was written.

**Throttling.** Two providers answer 429 with `Retry-After: 5` on roughly a quarter of requests. The
trigger is a per-client request counter shared across the whole API rather than a rate: bursting and
pacing produce the same share, three requests to another endpoint advance the same counter, and a
35-second pause does not reset it. Waiting longer buys nothing and exponential backoff would be
theatre; the stated delay is simply honoured.

**Empty objects.** An HTTP 200 carrying `{}` appeared 270 times, and exactly one of those runs lasted
more than a single cycle. It is a single-sample glitch, retried after one second.

**Server errors.** Polled every half second outside an incident, 5xx arrives as isolated blips: five
blips across four providers, none lasting longer than one request. Genuine incidents look nothing
like that, running 11 to 19 consecutive cycles. A retry two seconds later recovers the blip, and a
provider that already failed the previous cycle is not retried at all, so a service that is actually
down never sees its request rate doubled.

A stated delay always wins, including when it cannot be honoured: 504 asks for two minutes, so it is
left for the next cycle rather than retried now, and a `Retry-After` in a form this loop does not
parse also suppresses the retry. Substituting our own guess for an instruction we failed to read
would be worse than waiting.

Each retry is its own raw response with an `attempt` number, never a replacement, so collection
quality stays auditable, and two metrics are reported separately:

- `valid_percent`: share of attempts that produced an observation, attempt-level and unflattering;
- `data_percent`: share of polling cycles that produced data at all, which is what a reader means by
  "is this provider being collected".

A throttled attempt whose retry succeeded in the same cycle is not drawn on the error chart, because
throttling is not an error at all. A recovered 5xx stays on the chart: the provider did fail, and
erasing that would understate its instability. What neither of them does any more is imply lost data.

Outages are counted in polling cycles rather than requests: three consecutive cycles that produced no
data, so a cycle rescued by its retry is not an incident.

The dashboard exposes the last attempt and last valid observation separately. It also shows valid
percentage over the selected window, the current consecutive-failure streak, data freshness, p95
latency, and stacked error density. These are observations, not alert-policy decisions.

## Metric semantics

Canonical gauges use Prometheus-like names and bounded labels while SQLite remains the durable store:

- `provider_balance{provider, unit}`;
- `provider_credit{provider, unit}`;
- `provider_credits_remaining{provider}` with `capacity` and `refresh_at`;
- `provider_spend{provider, window}`.

Rates and projections are query-time read models, and they differ by pay model.

For balances and credit packages the displayed rate is the median slope measured across a fixed
ten-minute lag over up to 121 recent valid points. Adjacent-sample slopes cannot be used: a provider
that changes its value less often than we poll quantises them to zero, which reported no spend and
no runway for the slowest providers. Taking the median over many overlapping lagged pairs keeps the
estimate robust to isolated top-ups, package resets, and response gaps. Points are passed through a
three-sample median filter first, which removes single-sample glitches while preserving real steps.

For spend reports the metric is a cumulative total over a moving window rather than a balance, so
its slope measures how the window moves, not how much is being spent. The rate is the window level
divided by the window length, taken from a median of recent samples, and the projection is the
implied thirty-day spend at that pace. The slope of the window itself is still exposed separately as
`window_delta_per_hour`.

None of these are alerting thresholds or claims that the rate will persist.

Rates, forecasts and the burn headline are speedometers: they always read the most recent two
hours of samples (capped to the last 121 points), whatever range the reader has selected, so a
click that narrows the window to five minutes cannot turn them into five minutes of noise. The
selected range scopes attempts, trend, events and window spend. For a spend report the window
spend is the rate over the hours the series actually covers, never over hours with no data.

## Events

Levels alone force a reader to find top-ups, package resets, source glitches, and outages by eye, one
provider at a time. `events.py` derives them from the same canonical observations and processing
outcomes, so each one stays explainable from stored evidence and carries the `raw_response_id` it was
detected on:

- `top_up`: a rise that persists across the following samples;
- `package_reset`: the same, landing within 10% of the package capacity;
- `drawdown`: a persistent one-step fall;
- `spend_spike`: a spend window climbing past twice its own baseline, with the moment it returns;
- `glitch`: one sample off its neighbours that comes back on the next poll;
- `outage`: three or more consecutive failed attempts, grouped with their dominant cause.

A step must clear both a multiple of the series' own noise and a floor relative to its level, so a
jittery series and a coarse one both stay quiet. Events are observations, not alerts: nothing here
decides urgency.

## Screen model

Time is a single global dimension: the window is chosen once and every panel obeys it. Any chart
element can add a filter, so a reader can enter from the error chart or from a provider and end up in
the same state. Active filters are shown as removable chips and are mirrored in the URL, which makes
a view shareable and the back button meaningful.

The money lane is primary: totals, the event timeline, and the provider table ranked by hours to
zero. Collection quality is one chip in the header that expands into its own panel, so an
infrastructure question never outranks the spend question on first read.

## Alert policy

Alerts are states, not messages. Each rule declares which alerts should be open right now, and the
store is reconciled against that: a condition that became true opens once, a condition that stopped
being true resolves once, and nothing repeats every thirty seconds while a provider quietly runs out.
Every alert carries the evidence that makes it checkable, and `alerts.jsonl` records both transitions
as single-write append lines. Every line carries `ts` (ISO-8601 with a timezone offset) and `text`,
the keys the task requires, plus `event` (`opened`/`resolved`), `rule`, `severity`, `provider`,
`dedupe_key` and the evidence.

| rule | severity | condition |
| --- | --- | --- |
| `collector_dead` | page | no polling cycle for three minutes |
| `api_down` | page | every provider silent at once, raised instead of one alert each |
| `exhausted` | page | a balance crossed zero; for postpaid, debt began |
| `runway_1h` / `runway_5h` / `runway_24h` | page / today / fyi | predicted time to zero below the threshold |
| `provider_silent` | today above ten cycles, else fyi | consecutive polling cycles with no data |
| `spend_anomaly` | today | current burn is three times the provider's own four-hour baseline |
| `schema_drift` | today | a payload the adapter cannot read, or a provider with no adapter |

Severity states what the reader should do — wake someone, look today, or note it — rather than how
alarming it feels.

Four properties matter as much as the rules themselves:

- **Only the most urgent runway threshold is raised.** A provider with 1.4 hours left does not need
  three alerts saying so; as the runway shrinks the calmer alert resolves and the urgent one opens,
  which reads as an escalation.
- **A prediction does not flap.** An open runway alert stays open until the runway recovers past one
  and a half times its threshold.
- **Unknown is not zero.** A provider whose collection is broken is reported as blind, and no
  prediction is made through the blind spot.
- **Two writers, two responsibilities.** The collector evaluates the provider rules after each cycle
  and the read surface runs the watchdog, because a dead poller cannot report its own death. Each may
  resolve only the rules it can judge, so neither closes the other's alerts.

Warm-up is explicit: rate-based rules stay silent until a provider has twenty cycles of history, so a
restart never predicts from two samples.

## Public surface

The FastAPI service exposes only:

- `/`: static read-only dashboard;
- `/health`: process health;
- `/api/overview`: window overview, either `hours` or an explicit `start`/`end` range;
- `/api/providers/{provider}`: canonical series, classified outcomes, detected events, and the latest
  successful source payload with its raw response ID;
- `/api/raw/{raw_response_id}`: the stored response behind one plotted point, limited to the fields
  the dashboard already publishes;
- `/api/alerts`: the alerts that are open right now, worst first.

Headers, URLs, transport error text, and arbitrary database queries are not exposed publicly.
