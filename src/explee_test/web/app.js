const palette = {
  success: "#76e6bd",
  rate_limited: "#f1ba62",
  http_5xx: "#ff727d",
  http_other: "#ef8d62",
  payload: "#bc8cff",
  normalization: "#6fa8ff",
  transport: "#ed5fc9",
  throttled_recovered: "#4a5560",
};

const eventStyle = {
  top_up: { color: "#76e6bd", label: "top-up", group: "money" },
  package_reset: { color: "#6fa8ff", label: "package reset", group: "money" },
  drawdown: { color: "#f1ba62", label: "drawdown", group: "money" },
  spend_spike: { color: "#ff9d5c", label: "spend spike", group: "money" },
  glitch: { color: "#bc8cff", label: "source glitch", group: "collection" },
  outage: { color: "#ff727d", label: "outage", group: "collection" },
};

const ERROR_CATEGORIES = ["rate_limited", "http_5xx", "http_other", "payload", "normalization", "transport"];

const state = {
  hours: 12,
  start: null,
  end: null,
  provider: null,
  category: null,
  model: "",
  status: "",
  events: "money",
  sort: "risk",
  tab: "value",
  collection: false,
  alerts: false,
};

let overview;
let returnScroll = 0;
let qualityChart;
let valueChart;
let outcomeChart;

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function formatNumber(value, maximumFractionDigits = 2) {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("en", { maximumFractionDigits }).format(value);
}

function formatValue(value, unit) {
  if (value === null || value === undefined) return "—";
  const sign = value < 0 ? "-" : "";
  if (unit === "usd") return `${sign}$${formatNumber(Math.abs(value))}`;
  if (unit === "gbp") return `${sign}£${formatNumber(Math.abs(value))}`;
  return formatNumber(value, 0);
}

function formatHours(hours) {
  if (hours === null || hours === undefined) return "—";
  if (hours < 1) return `${Math.round(hours * 60)} min`;
  if (hours < 48) return `${formatNumber(hours, 1)} h`;
  return `${formatNumber(hours / 24, 1)} d`;
}

function clock(value) {
  return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function relative(seconds) {
  if (seconds === null || seconds === undefined) return "never";
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${(seconds / 3600).toFixed(1)}h ago`;
}

function escapeText(value) {
  return String(value).replace(/[&<>"]/g, (char) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[char]
  ));
}

/* ---------- state, URL, filters ---------- */

function readUrl() {
  const params = new URLSearchParams(location.search);
  const defaults = { provider: null, category: null, start: null, end: null, model: "", status: "" };
  for (const [key, fallback] of Object.entries(defaults)) {
    state[key] = params.get(key) || fallback;
  }
  state.events = params.get("events") || "money";
  state.sort = params.get("sort") || "risk";
  state.collection = params.get("collection") === "1";
  state.alerts = params.get("alerts") === "1";
  state.hours = Number(params.get("hours")) || 12;
  if (!state.provider) $("#detail").hidden = true;
  syncControls();
}

function writeUrl(push = false) {
  const params = new URLSearchParams();
  params.set("hours", state.hours);
  for (const key of ["provider", "category", "model", "status", "start", "end"]) {
    if (state[key]) params.set(key, state[key]);
  }
  if (state.events !== "money") params.set("events", state.events);
  if (state.sort !== "risk") params.set("sort", state.sort);
  if (state.collection) params.set("collection", "1");
  if (state.alerts) params.set("alerts", "1");
  const url = `${location.pathname}?${params}`;
  if (url === location.pathname + location.search) return;
  // Every deliberate move is a history entry, so Back walks the filters the reader
  // added instead of leaving the page altogether.
  if (push) history.pushState(null, "", url);
  else history.replaceState(null, "", url);
}

function windowQuery() {
  const params = new URLSearchParams({ hours: String(state.hours) });
  if (state.start) params.set("start", state.start);
  if (state.end) params.set("end", state.end);
  return params.toString();
}

function setFilter(patch, { reload = true, push = true } = {}) {
  Object.assign(state, patch);
  writeUrl(push);
  if (reload) load();
  else renderAll();
}

function resetView() {
  Object.assign(state, {
    hours: 12, start: null, end: null, provider: null, category: null,
    model: "", status: "", events: "money", sort: "risk", tab: "value",
    collection: false, alerts: false,
  });
  syncControls();
  $("#detail").hidden = true;
  $("#collection").hidden = true;
  $("#alerts-panel").hidden = true;
  writeUrl(true);
  load();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function syncControls() {
  $("#range").value = String(state.hours);
  $("#model-filter").value = state.model;
  $("#status-filter").value = state.status;
  $("#collection").hidden = !state.collection;
  $("#collection-chip").setAttribute("aria-expanded", String(state.collection));
  $("#alerts-panel").hidden = !state.alerts;
  $$("[data-events]").forEach((button) => button.classList.toggle("active", button.dataset.events === state.events));
}

function renderFilters() {
  const chips = [];
  if (state.start) {
    chips.push(["window", `${clock(state.start)} – ${clock(state.end || overview.range_end)}`, { start: null, end: null }]);
  }
  if (state.category) chips.push(["cause", state.category.replaceAll("_", " "), { category: null }]);
  if (state.provider) chips.push(["provider", state.provider, { provider: null }]);
  if (state.model) chips.push(["pay model", state.model.replaceAll("_", " "), { model: "" }]);
  if (state.status) chips.push(["status", state.status.replaceAll("_", " "), { status: "" }]);

  $("#filters").innerHTML = chips.length
    ? chips.map(([label, value], index) => `
      <button class="chip" type="button" data-chip="${index}">
        <span class="chip-label">${escapeText(label)}</span>${escapeText(value)}<span class="chip-x">×</span>
      </button>`).join("") + '<button class="chip chip-clear" type="button" data-chip="clear">clear all</button>'
    : "";
  $$("[data-chip]").forEach((chip) => chip.addEventListener("click", () => {
    if (chip.dataset.chip === "clear") {
      setFilter({ start: null, end: null, category: null, provider: null, model: "", status: "" });
      $("#model-filter").value = "";
      $("#status-filter").value = "";
      $("#detail").hidden = true;
      return;
    }
    const patch = chips[Number(chip.dataset.chip)][2];
    if (patch.provider === null) $("#detail").hidden = true;
    if ("model" in patch) $("#model-filter").value = "";
    if ("status" in patch) $("#status-filter").value = "";
    setFilter(patch, { reload: "start" in patch });
  }));
}

/* ---------- header ---------- */

function names(list) {
  if (list.length <= 3) return list.join(", ");
  return `${list.slice(0, 3).join(", ")} and ${list.length - 3} more`;
}

function runningOutCard(summary) {
  /* Being in debt already happened; reaching zero has not. One number for both
     reads as "nothing is wrong yet", which is the opposite of the truth. */
  const soon = summary.at_risk
    ? `${summary.at_risk} more reach zero within ${formatHours(summary.at_risk_hours)}: ${escapeText(names(summary.at_risk_providers))}`
    : `no others reach zero within ${formatHours(summary.at_risk_hours)}`;
  if (summary.in_debt) {
    return [
      "Out of money",
      `${summary.in_debt} in debt`,
      `${escapeText(names(summary.in_debt_providers))} · ${soon}`,
    ];
  }
  return [
    "Running out",
    summary.at_risk ? `${summary.at_risk} of ${summary.providers}` : "none",
    summary.at_risk
      ? `reach zero within ${formatHours(summary.at_risk_hours)}: ${escapeText(names(summary.at_risk_providers))}`
      : `nobody reaches zero within ${formatHours(summary.at_risk_hours)}`,
  ];
}

const severityLabel = { page: "page", today: "today", fyi: "fyi" };

function alertRow(alert, index, resolved) {
  const age = relative((Date.now() - new Date(alert.at)) / 1000);
  const state = resolved
    ? `closed after ${relative((new Date(alert.resolved_at) - new Date(alert.at)) / 1000).replace(" ago", "")}`
    : `opened ${age}`;
  return `<li class="alert alert-${escapeText(alert.severity)}${resolved ? " done" : ""}">
    <button type="button" data-alert="${index}" data-resolved="${resolved ? 1 : 0}">
      <span class="alert-severity">${escapeText(severityLabel[alert.severity] || alert.severity)}</span>
      <span class="alert-text">${escapeText(alert.text)}</span>
      <span class="alert-meta">${escapeText(alert.rule)} · ${state}</span>
    </button>
  </li>`;
}

function renderAlerts(alerts) {
  /* Only open alerts are ever listed, so nothing accumulates; the compact bar
     keeps even a busy incident down to one line until the reader asks for more. */
  const bar = $("#alert-bar");
  bar.hidden = !alerts.length;
  if (alerts.length) {
    const worst = alerts[0];
    const counts = alerts.reduce((acc, alert) => {
      acc[alert.severity] = (acc[alert.severity] || 0) + 1;
      return acc;
    }, {});
    const summary = ["page", "today", "fyi"]
      .filter((name) => counts[name])
      .map((name) => `${counts[name]} ${severityLabel[name]}`)
      .join(" · ");
    bar.className = `alert-bar alert-${worst.severity}`;
    bar.innerHTML = `
      <span class="alert-severity">${escapeText(severityLabel[worst.severity] || worst.severity)}</span>
      <span class="alert-text">${escapeText(worst.text)}</span>
      <span class="alert-meta">${escapeText(summary)} · ${state.alerts ? "hide" : "show all"}</span>`;
  }
  const panel = $("#alerts-panel");
  panel.hidden = !state.alerts;
  if (panel.hidden) return;
  $("#alerts").innerHTML = alerts.length
    ? alerts.map((alert, index) => alertRow(alert, index, false)).join("")
    : '<li class="event-empty">Nothing is open right now.</li>';
  const history = (overview.alerts_recent || []).filter((item) => item.resolved_at);
  $("#alerts-history").innerHTML = history.length
    ? history.map((alert, index) => alertRow(alert, index, true)).join("")
    : '<li class="event-empty">Nothing opened and closed in this window.</li>';
  $$("[data-alert]").forEach((node) => node.addEventListener("click", () => {
    const list = node.dataset.resolved === "1" ? history : alerts;
    const alert = list[Number(node.dataset.alert)];
    if (alert.provider) setFilter({ provider: alert.provider, tab: "value" });
  }));
}

function windowLabel(hours) {
  if (!hours) return "the selected window";
  if (hours < 1) return `the selected ${Math.round(hours * 60)} min`;
  if (hours < 48) return `the selected ${formatNumber(hours, hours < 3 ? 1 : 0)} h`;
  return `the selected ${formatNumber(hours / 24, 1)} d`;
}

function renderAnswer(summary) {
  /* The rate is a speedometer over its own short window; the range selector scopes
     everything else. Saying both out loud is the whole point, because a number
     that ignores the selector without saying so reads as broken. */
  const measured = summary.rate_window_minutes
    ? `measured over the last ${formatNumber(summary.rate_window_minutes, 0)} min`
    : "not enough samples yet";
  const covered = summary.window_covered_hours;
  const partial = covered && covered < overview.range_hours * 0.95
    ? ` (${formatNumber(covered, 1)} h of data)`
    : "";
  const period = summary.window_spend_usd
    ? `In ${windowLabel(overview.range_hours)}${partial}: ${formatValue(summary.window_spend_usd, "usd")} spent, ${formatValue(summary.window_spend_per_hour, "usd")}/h on average`
    : `No spend recorded in ${windowLabel(overview.range_hours)}`;
  const cards = [
    [
      "Burning now",
      `${formatValue(summary.usd_burn_per_hour, "usd")}/h`,
      `${measured} · ${summary.usd_sources} USD-denominated sources`,
      period,
    ],
    [
      "Projected 30 days",
      formatValue(summary.usd_projected_30d, "usd"),
      "at the current pace, not a commitment",
    ],
    runningOutCard(summary),
  ];
  $("#answer").innerHTML = cards.map(([label, value, note, extra], index) => `
    <article class="answer-card${index === 2 && (summary.at_risk || summary.in_debt) ? " warn" : ""}">
      <div class="summary-label">${label}</div>
      <div class="summary-value">${value}</div>
      <div class="summary-note">${note}</div>
      ${extra ? `<div class="summary-note period">${extra}</div>` : ""}
    </article>`).join("");

  const chip = $("#collection-chip");
  const degraded = summary.degraded;
  const retried = summary.throttled_recovered ? ` · ${summary.throttled_recovered} throttled, retried` : "";
  chip.textContent = `Collection ${formatNumber(summary.valid_percent, 1)}% valid${retried} · p95 ${formatNumber(summary.p95_latency_ms, 0)} ms${degraded ? ` · ${degraded} degraded` : ""}`;
  chip.classList.toggle("bad", Boolean(degraded));
}

/* ---------- events ---------- */

function visibleEvents() {
  const kinds = Object.entries(eventStyle)
    .filter(([, style]) => state.events === "all" || style.group === state.events)
    .map(([kind]) => kind);
  return overview.events.filter((event) => (
    kinds.includes(event.kind) && (!state.provider || event.provider === state.provider)
  ));
}

function renderEvents() {
  const events = visibleEvents();
  const from = new Date(overview.range_start).getTime();
  const to = new Date(overview.range_end).getTime();
  const span = Math.max(to - from, 1);

  $("#event-strip").innerHTML = events.map((event, index) => {
    const style = eventStyle[event.kind];
    const left = ((new Date(event.at).getTime() - from) / span) * 100;
    const width = event.ended_at
      ? Math.max(0.4, ((new Date(event.ended_at) - new Date(event.at)) / span) * 100)
      : 0.4;
    return `<button class="tick" type="button" data-event="${index}"
      style="left:${Math.min(99.6, Math.max(0, left))}%;width:${width}%;background:${style.color}"
      title="${escapeText(`${clock(event.at)} · ${event.provider} · ${style.label} — ${event.detail}`)}"></button>`;
  }).join("");
  $("#strip-axis").innerHTML = `<span>${clock(overview.range_start)}</span><span>${clock(overview.range_end)}</span>`;

  $("#event-list").innerHTML = events.slice(0, 6).map((event, index) => {
    const style = eventStyle[event.kind];
    return `<li><button type="button" data-event="${index}">
      <span class="dot" style="background:${style.color}"></span>
      <span class="event-time">${clock(event.at)}</span>
      <span class="event-provider">${escapeText(event.provider)}</span>
      <span class="event-kind">${style.label}</span>
      <span class="event-detail">${escapeText(event.detail)}</span>
    </button></li>`;
  }).join("") || '<li class="event-empty">No events of this kind in the window.</li>';

  $$("[data-event]").forEach((node) => node.addEventListener("click", () => {
    const event = events[Number(node.dataset.event)];
    focusEvent(event);
  }));
}

function focusEvent(event) {
  const at = new Date(event.at).getTime();
  const ended = event.ended_at ? new Date(event.ended_at).getTime() : at;
  const pad = Math.max(15 * 60 * 1000, (ended - at) * 0.5);
  setFilter({
    provider: event.provider,
    start: new Date(at - pad).toISOString(),
    end: new Date(ended + pad).toISOString(),
    tab: event.raw_response_id ? "value" : "attempts",
  });
}

/* ---------- providers ---------- */

function sparkline(values) {
  if (!values || values.length < 2) return "";
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const points = values.map((value, index) => {
    const x = (index / (values.length - 1)) * 100;
    const y = 26 - ((value - min) / span) * 22;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const trend = values[values.length - 1] >= values[0] ? "var(--accent)" : "var(--amber)";
  return `<svg class="spark" viewBox="0 0 100 28" preserveAspectRatio="none" aria-hidden="true">
    <polyline points="${points}" fill="none" stroke="${trend}" stroke-width="1.6" vector-effect="non-scaling-stroke"/>
  </svg>`;
}

function riskCell(item) {
  const forecast = item.forecast;
  if (!forecast) return '<span class="muted">—</span>';
  if (forecast.kind === "in_debt") {
    return `<span class="metric-value bad-text">in debt</span><span class="subvalue">${formatValue(forecast.value, item.unit)}</span>`;
  }
  if (forecast.kind === "projected_30d") {
    return `<span class="muted">n/a</span><span class="subvalue">spend report</span>`;
  }
  if (forecast.kind === "refresh_first") {
    return `<span class="muted">survives</span><span class="subvalue">${formatValue(forecast.value, item.unit)} left at refresh</span>`;
  }
  const urgent = forecast.hours <= 48;
  const note = forecast.kind === "before_refresh"
    ? `empties before refresh in ${formatHours(forecast.refresh_hours)}`
    : "at the current rate";
  return `<span class="metric-value${urgent ? " bad-text" : ""}">${formatHours(forecast.hours)}</span><span class="subvalue">${note}</span>`;
}

function rateCell(item) {
  if (item.rate_per_hour === null || item.rate_per_hour === undefined) return '<span class="muted">—</span>';
  const primary = `${formatValue(item.rate_per_hour, item.unit)}/h`;
  if (item.rate_kind !== "spend") return `<span class="metric-value">${primary}</span>`;
  const delta = item.window_delta_per_hour === null || item.window_delta_per_hour === undefined
    ? ""
    : ` · window ${item.window_delta_per_hour > 0 ? "+" : ""}${formatValue(item.window_delta_per_hour, item.unit)}/h`;
  return `<span class="metric-value">${primary} spent</span><span class="subvalue">${formatNumber(item.spend_window_hours, 0)}h window${delta}</span>`;
}

function matchesFilters(item) {
  if (state.model && item.pay_model !== state.model) return false;
  if (state.provider && item.provider !== state.provider) return false;
  if (state.category && !(item.outcomes || {})[state.category]) return false;
  if (state.status === "at_risk" && !(item.risk_hours !== null && item.risk_hours <= overview.summary.at_risk_hours)) return false;
  if (state.status === "degraded" && ["success", "throttled"].includes(item.last_attempt?.outcome)) return false;
  if (state.status === "stale" && !(item.freshness_seconds === null || item.freshness_seconds > 90)) return false;
  return true;
}

const sorters = {
  name: (a, b) => a.name.localeCompare(b.name),
  value: (a, b) => (b.value ?? -Infinity) - (a.value ?? -Infinity),
  rate: (a, b) => Math.abs(b.rate_per_hour ?? 0) - Math.abs(a.rate_per_hour ?? 0),
  relative: (a, b) => (a.rate_percent_per_hour ?? 0) - (b.rate_percent_per_hour ?? 0),
  risk: (a, b) => (a.risk_hours ?? Infinity) - (b.risk_hours ?? Infinity),
  status: (a, b) => (a.data_percent ?? 100) - (b.data_percent ?? 100),
};

function renderProviders() {
  const items = overview.providers.filter(matchesFilters).sort(sorters[state.sort] || sorters.risk);
  $("#table-empty").hidden = items.length > 0;
  const minutes = overview.summary.rate_window_minutes;
  $("#table-caption").textContent = minutes
    ? `Rate and time to zero are measured over the last ${formatNumber(minutes, 0)} minutes, so they answer "right now" whatever range is selected. Current, trend and cycles follow ${windowLabel(overview.range_hours)}.`
    : "";
  $("#providers").innerHTML = items.map((item) => {
    const good = ["success", "throttled"].includes(item.last_attempt?.outcome);
    const capacity = item.capacity
      ? `${formatNumber((100 * item.value) / item.capacity, 1)}% of package`
      : item.pay_model.replaceAll("_", " ");
    const streak = item.failure_streak.count > 2 ? ` · ${item.failure_streak.count} in a row` : "";
    const coverage = item.data_percent === null ? "no data" : `${formatNumber(item.data_percent, 1)}%`;
    const throttled = item.throttled_recovered
      ? `${item.throttled_recovered} throttled, retried`
      : relative(item.freshness_seconds);
    return `<tr tabindex="0" data-provider="${item.provider}"${item.provider === state.provider ? ' class="selected"' : ""}>
      <td><span class="provider-name">${escapeText(item.name)}</span><span class="provider-id">${escapeText(item.provider)}</span></td>
      <td><span class="metric-value">${formatValue(item.value, item.unit)}</span><span class="subvalue">${capacity}</span></td>
      <td>${rateCell(item)}</td>
      <td>${item.rate_percent_per_hour === null ? '<span class="muted">—</span>' : `<span class="metric-value">${formatNumber(item.rate_percent_per_hour, 2)}%</span>`}</td>
      <td>${riskCell(item)}</td>
      <td class="spark-cell">${sparkline(item.sparkline)}</td>
      <td><span class="pill ${good ? "good" : "bad"}">${coverage}${streak}</span><span class="subvalue">${throttled}</span></td>
    </tr>`;
  }).join("");

  $$("#providers tr").forEach((row) => {
    const open = () => {
      returnScroll = window.scrollY;
      state.provider = row.dataset.provider;
      writeUrl(true);
      showProvider(row.dataset.provider).then(() => {
        $("#detail").scrollIntoView({ behavior: "smooth", block: "start" });
      });
      renderFilters();
    };
    row.addEventListener("click", open);
    row.addEventListener("keydown", (event) => { if (event.key === "Enter") open(); });
  });
  $$("th[data-sort]").forEach((head) => {
    head.classList.toggle("sorted", head.dataset.sort === state.sort);
  });
}

/* ---------- collection lane ---------- */

function observedCategories(rows, candidates) {
  /* A cause that has never happened is still monitored, but drawing it puts an
     empty legend entry in front of the reader and invites the question of what
     they are looking at. It reappears on its own the moment it occurs once. */
  return candidates.filter((name) => rows.some((row) => (row[name] || 0) > 0));
}

function renderQuality(rows) {
  if (!window.echarts) return;
  const categories = observedCategories(rows, ERROR_CATEGORIES);
  qualityChart ||= echarts.init($("#quality-chart"));
  qualityChart.setOption({
    animationDuration: 300,
    color: categories.map((name) => palette[name]),
    tooltip: { trigger: "axis", backgroundColor: "#181d23", borderColor: "#35404a", textStyle: { color: "#edf2f5" } },
    legend: { top: 8, textStyle: { color: "#8e9aa6" }, data: [...categories, "valid %"] },
    grid: { left: 48, right: 56, top: 48, bottom: 42 },
    xAxis: { type: "time", axisLine: { lineStyle: { color: "#34404a" } }, axisLabel: { color: "#8e9aa6" } },
    yAxis: [
      { type: "value", name: "failed", minInterval: 1, nameTextStyle: { color: "#8e9aa6" }, splitLine: { lineStyle: { color: "#232a31" } }, axisLabel: { color: "#8e9aa6" } },
      { type: "value", name: "valid %", max: 100, min: 0, position: "right", splitLine: { show: false }, nameTextStyle: { color: "#8e9aa6" }, axisLabel: { color: "#8e9aa6" } },
    ],
    series: [
      ...categories.map((name) => ({
        name,
        type: "bar",
        stack: "errors",
        emphasis: { focus: "series" },
        data: rows.map((row) => [row.at, row[name] || 0]),
      })),
      {
        name: "valid %",
        type: "line",
        yAxisIndex: 1,
        showSymbol: false,
        lineStyle: { width: 1.5, color: palette.success },
        itemStyle: { color: palette.success },
        data: rows.map((row) => [row.at, row.valid_percent]),
      },
    ],
  }, true);
  qualityChart.off("click");
  qualityChart.on("click", (params) => {
    if (!params.value || params.seriesName === "valid %") return;
    const at = new Date(params.value[0]).getTime();
    const bucket = rows.length > 1 ? new Date(rows[1].at) - new Date(rows[0].at) : 300000;
    setFilter({
      start: new Date(at).toISOString(),
      end: new Date(at + bucket).toISOString(),
      category: params.seriesName,
    });
  });
}

/* ---------- provider detail ---------- */

function redundantSeries(series) {
  /* A trailing_30d spend window is trailing_24h times a constant in this source,
     so it is plotted only on request instead of shadowing the real curve. */
  const skip = new Set();
  const base = series.find((item) => item.name.includes("trailing_24h"));
  if (!base) return skip;
  for (const item of series) {
    if (item === base || item.points.length !== base.points.length) continue;
    const ratios = item.points
      .map((point, index) => (base.points[index][1] ? point[1] / base.points[index][1] : null))
      .filter((ratio) => ratio !== null);
    if (!ratios.length) continue;
    const first = ratios[0];
    if (ratios.every((ratio) => Math.abs(ratio / first - 1) < 0.001)) skip.add(item.name);
  }
  return skip;
}

function renderValueChart(series, events) {
  if (!window.echarts) return;
  valueChart ||= echarts.init($("#value-chart"));
  // One label per kind: five glitch marks in a row would otherwise overprint.
  const labelled = new Set();
  const marks = events
    .filter((event) => event.kind !== "outage")
    .map((event) => {
      const style = eventStyle[event.kind];
      const first = !labelled.has(event.kind);
      labelled.add(event.kind);
      return {
        xAxis: event.at,
        lineStyle: { color: style.color, type: "dashed", width: 1.2 },
        label: first
          ? { show: true, formatter: style.label, color: style.color, fontSize: 10, rotate: 90, position: "insideEndTop" }
          : { show: false },
      };
    });
  const redundant = redundantSeries(series);
  const axes = series.map((item, index) => ({
    type: "value",
    position: index % 2 ? "right" : "left",
    offset: Math.floor(index / 2) * 54,
    name: item.unit,
    nameTextStyle: { color: "#8e9aa6" },
    axisLabel: { color: "#8e9aa6" },
    splitLine: { show: index === 0, lineStyle: { color: "#29313a" } },
  }));
  valueChart.setOption({
    color: ["#76e6bd", "#6fa8ff", "#f1ba62"],
    tooltip: { trigger: "axis", backgroundColor: "#181d23", borderColor: "#35404a", textStyle: { color: "#edf2f5" } },
    legend: {
      textStyle: { color: "#8e9aa6" },
      selected: Object.fromEntries(series.map((item) => [item.name, !redundant.has(item.name)])),
    },
    grid: { left: 62, right: series.length > 1 ? 70 : 28, top: 42, bottom: 44 },
    xAxis: { type: "time", axisLabel: { color: "#8e9aa6" }, axisLine: { lineStyle: { color: "#34404a" } } },
    yAxis: axes.length ? axes : [{ type: "value" }],
    dataZoom: [{ type: "inside" }],
    series: series.map((item, index) => ({
      name: item.name,
      type: "line",
      yAxisIndex: index,
      showSymbol: false,
      connectNulls: false,
      data: item.points,
      markLine: index === 0 && marks.length ? { symbol: "none", silent: true, data: marks } : undefined,
    })),
  }, true);
  valueChart.off("click");
  valueChart.on("click", (params) => {
    const rawId = params.value?.[2];
    if (rawId) showRaw(rawId);
  });
}

function renderOutcomeChart(attempts) {
  if (!window.echarts) return;
  const seen = new Set(attempts.map((item) => item.category));
  const categories = ["success", "throttled_recovered", ...ERROR_CATEGORIES].filter(
    (name) => seen.has(name),
  );
  outcomeChart ||= echarts.init($("#outcome-chart"));
  outcomeChart.setOption({
    tooltip: { formatter: (item) => `${new Date(item.value[0]).toLocaleString()}<br>${item.data.outcome}${item.data.status_code ? ` · HTTP ${item.data.status_code}` : ""}${item.data.attempt > 1 ? ` · retry ${item.data.attempt}` : ""}` },
    grid: { left: 96, right: 24, top: 18, bottom: 44 },
    xAxis: { type: "time", axisLabel: { color: "#8e9aa6" }, axisLine: { lineStyle: { color: "#34404a" } } },
    yAxis: { type: "category", data: categories, axisLabel: { color: "#8e9aa6" }, axisLine: { lineStyle: { color: "#34404a" } } },
    dataZoom: [{ type: "inside" }],
    series: [{
      type: "scatter",
      symbolSize: 6,
      data: attempts.map((item) => ({
        value: [item.at, item.category],
        outcome: item.outcome,
        status_code: item.status_code,
        attempt: item.attempt,
        itemStyle: { color: palette[item.category] },
      })),
    }],
  }, true);
}

async function showRaw(rawResponseId) {
  const response = await fetch(`/api/raw/${rawResponseId}`);
  if (!response.ok) return;
  const data = await response.json();
  selectTab("raw");
  $("#raw-meta").textContent = `raw_response_id ${data.raw_response_id} · ${new Date(data.requested_at).toLocaleString()} · HTTP ${data.status_code ?? "—"} · ${data.outcome ?? "unprocessed"}`;
  $("#raw-payload").textContent = data.payload ? JSON.stringify(data.payload, null, 2) : "not a JSON body";
}

function selectTab(tab) {
  state.tab = tab;
  $$("[data-tab]").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
  $$("[data-pane]").forEach((pane) => { pane.hidden = pane.dataset.pane !== tab; });
  if (tab === "value") valueChart?.resize();
  if (tab === "attempts") outcomeChart?.resize();
}

async function showProvider(provider) {
  const response = await fetch(`/api/providers/${provider}?${windowQuery()}`);
  if (!response.ok) return;
  const data = await response.json();
  $("#detail").hidden = false;
  $("#detail-kind").textContent = `${data.provider.pay_model.replaceAll("_", " ")} · ${data.provider.provider}`;
  $("#detail-title").textContent = data.provider.name;
  renderValueChart(data.series, data.events);
  renderOutcomeChart(data.attempts);
  selectTab(state.tab === "raw" ? "value" : state.tab);
  if (data.latest_raw) {
    $("#raw-meta").textContent = `raw_response_id ${data.latest_raw.raw_response_id} · ${new Date(data.latest_raw.requested_at).toLocaleString()} · latest valid`;
    $("#raw-payload").textContent = JSON.stringify(data.latest_raw.payload, null, 2);
  } else {
    $("#raw-meta").textContent = "No successful source payload in this window";
    $("#raw-payload").textContent = "—";
  }
  renderProviders();
  renderEvents();
}

/* ---------- load ---------- */

function renderAll() {
  renderAnswer(overview.summary);
  renderAlerts(overview.alerts || []);
  renderFilters();
  renderEvents();
  renderProviders();
  renderQuality(overview.quality_series);
  $("#updated").textContent = `Updated ${new Date(overview.generated_at).toLocaleTimeString()}`;
}

async function load() {
  try {
    const response = await fetch(`/api/overview?${windowQuery()}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    overview = await response.json();
    renderAll();
    if (state.provider) await showProvider(state.provider);
  } catch (error) {
    $("#answer").innerHTML = `<div class="error-state">Dashboard data unavailable: ${escapeText(error.message)}</div>`;
  }
}

$("#range").addEventListener("change", (event) => {
  setFilter({ hours: Number(event.target.value), start: null, end: null });
});
$("#model-filter").addEventListener("change", (event) => setFilter({ model: event.target.value }, { reload: false }));
$("#status-filter").addEventListener("change", (event) => setFilter({ status: event.target.value }, { reload: false }));
$$("[data-events]").forEach((button) => button.addEventListener("click", () => {
  $$("[data-events]").forEach((other) => other.classList.toggle("active", other === button));
  setFilter({ events: button.dataset.events }, { reload: false });
}));
$$("[data-tab]").forEach((button) => button.addEventListener("click", () => selectTab(button.dataset.tab)));
$$("th[data-sort]").forEach((head) => head.addEventListener("click", () => setFilter({ sort: head.dataset.sort }, { reload: false })));
$("#collection-chip").addEventListener("click", () => {
  const panel = $("#collection");
  panel.hidden = !panel.hidden;
  state.collection = !panel.hidden;
  writeUrl(true);
  $("#collection-chip").setAttribute("aria-expanded", String(state.collection));
  if (state.collection) {
    qualityChart?.resize();
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
});
function closeDetail() {
  $("#detail").hidden = true;
  setFilter({ provider: null }, { reload: false });
  window.scrollTo({ top: returnScroll, behavior: "smooth" });
}

function closeCollection() {
  $("#collection").hidden = true;
  state.collection = false;
  writeUrl(true);
  $("#collection-chip").setAttribute("aria-expanded", "false");
}

function toggleAlerts(open) {
  state.alerts = open;
  writeUrl(true);
  renderAlerts(overview.alerts || []);
  if (open) $("#alerts-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

$("#alert-bar").addEventListener("click", () => toggleAlerts(!state.alerts));
$("#close-alerts").addEventListener("click", () => toggleAlerts(false));
$("#close-detail").addEventListener("click", closeDetail);
$("#close-collection").addEventListener("click", closeCollection);
$("#home").addEventListener("click", resetView);
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!$("#detail").hidden) closeDetail();
  else if (!$("#alerts-panel").hidden) toggleAlerts(false);
  else if (!$("#collection").hidden) closeCollection();
  else resetView();
});
window.addEventListener("popstate", () => {
  readUrl();
  load();
});
window.addEventListener("resize", () => {
  qualityChart?.resize();
  valueChart?.resize();
  outcomeChart?.resize();
});

readUrl();
let waited = 0;
const waitForCharts = setInterval(() => {
  if (window.echarts) {
    clearInterval(waitForCharts);
    load();
    setInterval(() => { if (!state.start) load(); }, 30_000);
  } else if ((waited += 50) > 8000) {
    clearInterval(waitForCharts);
    $("#answer").innerHTML = '<div class="error-state">Chart library did not load. Tables and events still work after a reload.</div>';
    load();
  }
}, 50);
