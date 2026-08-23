"""Detection of the discrete events an analyst looks for in a provider series.

A dashboard that only plots levels forces the reader to find top-ups, package
resets, source glitches, and outages by eye, one provider at a time. These
detectors turn those shapes into addressable events so a single timeline can
answer "what happened, where, and when".

Nothing here decides urgency: an event is an observation about the series, not an
alert. Detection runs over canonical observations and processing outcomes only,
so every event stays explainable from stored evidence.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from typing import Any

# A step must clear both a multiple of the series' own noise and a floor relative
# to its level, so that neither a jittery series nor a coarse one fires forever.
GLITCH_NOISE_FACTOR = 6.0
STEP_NOISE_FACTOR = 10.0
MIN_RELATIVE_CHANGE = 0.005
LEVEL_SPAN = 3
PERSISTENCE_SHARE = 0.6
SPEND_SPIKE_FACTOR = 2.0
MIN_OUTAGE_CYCLES = 3

EVENT_KINDS = (
    "top_up",
    "package_reset",
    "drawdown",
    "spend_spike",
    "glitch",
    "outage",
)


@dataclass(frozen=True)
class Event:
    """One discrete thing that happened to a provider, with its evidence."""

    at: str
    provider: str
    kind: str
    detail: str
    ended_at: str | None = None
    magnitude: float | None = None
    unit: str | None = None
    raw_response_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


Point = tuple[str, float, int | None]


def _median_filtered(values: list[float]) -> list[float]:
    if len(values) < 3:
        return list(values)
    inner = [
        statistics.median((left, middle, right))
        for left, middle, right in zip(values, values[1:], values[2:], strict=False)
    ]
    return [values[0], *inner, values[-1]]


def _noise(values: list[float]) -> float:
    steps = [abs(right - left) for left, right in zip(values, values[1:], strict=False)]
    return statistics.median(steps) if steps else 0.0


def _threshold(noise: float, factor: float, level: float) -> float:
    return max(noise * factor, abs(level) * MIN_RELATIVE_CHANGE)


def _level(values: list[float], start: int, stop: int) -> float:
    window = values[max(0, start) : max(0, stop)]
    return statistics.median(window) if window else 0.0


def detect_series_events(
    provider: str,
    pay_model: str,
    unit: str,
    points: list[Point],
    capacity: float | None = None,
) -> list[Event]:
    """Find glitches, level steps, and spend spikes in one canonical series."""

    if len(points) < 5:
        return []
    values = [value for _, value, _ in points]
    smooth = _median_filtered(values)
    noise = _noise(smooth)
    events: list[Event] = []

    for index in range(1, len(values) - 1):
        neighbourhood = statistics.median((values[index - 1], values[index], values[index + 1]))
        deviation = values[index] - neighbourhood
        if abs(deviation) > _threshold(noise, GLITCH_NOISE_FACTOR, neighbourhood):
            events.append(
                Event(
                    at=points[index][0],
                    provider=provider,
                    kind="glitch",
                    detail=(
                        f"single sample off by {deviation:+.2f} and back on the next poll"
                    ),
                    magnitude=round(deviation, 4),
                    unit=unit,
                    raw_response_id=points[index][2],
                )
            )

    if pay_model == "spend_report":
        events.extend(_spend_spikes(provider, unit, points, smooth))
        return events

    for index in range(len(smooth) - 1):
        delta = smooth[index + 1] - smooth[index]
        if abs(delta) <= _threshold(noise, STEP_NOISE_FACTOR, smooth[index]):
            continue
        before = _level(smooth, index + 1 - LEVEL_SPAN, index + 1)
        after = _level(smooth, index + 1, index + 1 + LEVEL_SPAN)
        shift = after - before
        if abs(shift) < abs(delta) * PERSISTENCE_SHARE or (shift > 0) != (delta > 0):
            continue  # the level came back; already reported as a glitch
        at, _, raw_response_id = points[index + 1]
        if shift > 0:
            reset = (
                pay_model == "credits_package" and capacity is not None and after >= capacity * 0.9
            )
            events.append(
                Event(
                    at=at,
                    provider=provider,
                    kind="package_reset" if reset else "top_up",
                    detail=(
                        f"level rose {shift:+.2f} to {after:.2f} and stayed there"
                        if not reset
                        else f"package refilled to {after:.2f} of {capacity:.2f}"
                    ),
                    magnitude=round(shift, 4),
                    unit=unit,
                    raw_response_id=raw_response_id,
                )
            )
        else:
            events.append(
                Event(
                    at=at,
                    provider=provider,
                    kind="drawdown",
                    detail=f"level dropped {shift:+.2f} to {after:.2f} in one step",
                    magnitude=round(shift, 4),
                    unit=unit,
                    raw_response_id=raw_response_id,
                )
            )
    return events


def _spend_spikes(
    provider: str, unit: str, points: list[Point], smooth: list[float]
) -> list[Event]:
    """A spend window that climbs far above its own baseline and comes back."""

    baseline = statistics.median(smooth)
    if baseline <= 0:
        return []
    limit = baseline * SPEND_SPIKE_FACTOR
    events = []
    start: int | None = None
    for index, value in enumerate([*smooth, 0.0]):
        above = value > limit
        if above and start is None:
            start = index
        elif not above and start is not None:
            peak = max(smooth[start:index])
            events.append(
                Event(
                    at=points[start][0],
                    provider=provider,
                    kind="spend_spike",
                    detail=(
                        f"spend window peaked at {peak:.2f}, "
                        f"{peak / baseline:.1f}x its baseline of {baseline:.2f}"
                    ),
                    ended_at=points[min(index, len(points) - 1)][0],
                    magnitude=round(peak, 4),
                    unit=unit,
                    raw_response_id=points[start][2],
                )
            )
            start = None
    return events


def detect_outages(provider: str, cycles: list[tuple[str, str, str | None]]) -> list[Event]:
    """Group consecutive polling cycles that produced no data into one outage each.

    ``cycles`` is ordered oldest first as (at, outcome, category), one entry per
    polling cycle rather than per request: a cycle whose retry succeeded delivered
    its data and is not an incident. Throttled cycles are skipped entirely, because
    a provider that answers "retry in five seconds" is available, and counting that
    as an outage invents incidents that never happened.
    """

    events = []
    run: list[tuple[str, str, str | None]] = []

    def flush() -> None:
        if len(run) < MIN_OUTAGE_CYCLES:
            return
        categories = [category for _, _, category in run if category]
        dominant = max(set(categories), key=categories.count) if categories else "failure"
        events.append(
            Event(
                at=run[0][0],
                provider=provider,
                kind="outage",
                detail=f"{len(run)} consecutive polls without data, mostly {dominant}",
                ended_at=run[-1][0],
                magnitude=float(len(run)),
            )
        )

    for attempt in cycles:
        if attempt[2] == "rate_limited":
            continue  # a stated delay is neither a failure nor a recovery
        if attempt[1] == "success":
            flush()
            run = []
        else:
            run.append(attempt)
    flush()
    return events
