"""A shared cache of the read projections.

The dashboard is read-only and every viewer asks the same handful of questions of
the same capture, which the collector extends once every thirty seconds. Building
a projection per request therefore repeats work that cannot have changed, and on a
single-core host the repeats queue behind each other: the widest window is the most
expensive one to build and the one most likely to be asked for again while an
earlier build is still running.

So a projection is built once and handed to everyone who asks for it until the
capture has moved on. What "moved on" means is measured rather than assumed: a
projection is rebuilt no more often than the collector writes, and no more often
than a small multiple of what it costs to build, so an expensive window cannot
spend the host on refreshing itself.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
from concurrent.futures import Executor
from dataclasses import dataclass
from typing import Any

# The collector writes every thirty seconds, so nothing is gained by rebuilding
# faster than that.
MINIMUM_AGE_SECONDS = 30.0
# An answer that takes a second may be rebuilt every twenty; one that takes six may
# not. The host has one core, so a rebuild is time no request can be served in, and
# the wider the window the less a poll cycle changes it: a week moves by 0.3% while
# the projection of it is two minutes old, which is not worth stalling viewers for.
REFRESH_FACTOR = 20.0
# Past this age a cached answer is no longer served: the caller waits for a fresh
# one. This is what a viewer sees if the refresher is not running at all.
STALE_AFTER_SECONDS = 120.0
# How long a window nobody asked for keeps being refreshed after the last request.
DEMAND_SECONDS = 600.0


@dataclass
class _Entry:
    payload: Any
    built_at: float
    build_seconds: float
    requested_at: float


class ProjectionCache:
    """Build a projection at most as often as it is worth rebuilding."""

    def __init__(
        self,
        build: Callable[..., Any],
        *,
        executor: Executor | None = None,
        warm: tuple[tuple, ...] = (),
        minimum_age: float = MINIMUM_AGE_SECONDS,
        refresh_factor: float = REFRESH_FACTOR,
        stale_after: float = STALE_AFTER_SECONDS,
        demand_seconds: float = DEMAND_SECONDS,
    ) -> None:
        self._build = build
        self._executor = executor
        self._warm = set(warm)
        self._minimum_age = minimum_age
        self._refresh_factor = refresh_factor
        self._stale_after = stale_after
        self._demand_seconds = demand_seconds
        self._entries: dict[Hashable, _Entry] = {}
        self._guard = threading.Lock()
        # One build per key at a time: without this, two viewers arriving together
        # on a cold window each pay the full cost and the host pays twice.
        self._building: dict[Hashable, threading.Lock] = {}

    def build_elsewhere(self, executor: Executor | None) -> None:
        """Build in another process rather than this one.

        A projection is seconds of work that holds the interpreter lock, and this
        process also has to answer requests that are already in memory. Sharing a
        core with the build is survivable; sharing an interpreter lock with it is
        not, and it showed as the page freezing for as long as a rebuild took.
        """

        self._executor = executor

    def keep_warm(self, keys: tuple[tuple, ...]) -> None:
        """Name the keys to keep built even when nobody is asking for them."""

        with self._guard:
            self._warm = set(keys)

    def get(self, key: tuple) -> Any:
        now = time.monotonic()
        with self._guard:
            entry = self._entries.get(key)
            if entry is not None:
                entry.requested_at = now
                if now - entry.built_at < self._stale_after:
                    return entry.payload
            lock = self._building.setdefault(key, threading.Lock())
        with lock:
            # Another caller may have finished the build while this one waited.
            with self._guard:
                entry = self._entries.get(key)
                if entry is not None and time.monotonic() - entry.built_at < self._stale_after:
                    return entry.payload
            # A caller is already waiting on this one, so it is built here: handing
            # it to the builder would only add the trip there and back.
            return self._rebuild(key, elsewhere=False)

    def refresh_due(self) -> list[tuple]:
        """Keys worth rebuilding now, the most overdue first.

        The caller rebuilds one at a time, so the order is the whole answer to which
        projection has been waiting longest relative to how often it asked to be
        rebuilt. A key with nothing built yet has waited forever.
        """

        now = time.monotonic()
        due: list[tuple[float, tuple]] = []
        with self._guard:
            for key in list(self._entries):
                entry = self._entries[key]
                in_demand = (
                    key in self._warm or now - entry.requested_at <= self._demand_seconds
                )
                if not in_demand:
                    del self._entries[key]
                    self._building.pop(key, None)
                    continue
                interval = max(self._minimum_age, self._refresh_factor * entry.build_seconds)
                overdue = (now - entry.built_at) / interval
                if overdue >= 1.0:
                    due.append((overdue, key))
            missing = [key for key in self._warm if key not in self._entries]
        due.sort(key=lambda item: item[0], reverse=True)
        return missing + [key for _, key in due]

    def rebuild(self, key: tuple) -> None:
        """Refresh one key, holding its build lock so a request does not duplicate it."""

        with self._guard:
            lock = self._building.setdefault(key, threading.Lock())
        with lock:
            self._rebuild(key, elsewhere=True)

    def _rebuild(self, key: tuple, *, elsewhere: bool) -> Any:
        started = time.monotonic()
        if elsewhere and self._executor is not None:
            payload = self._executor.submit(self._build, *key).result()
        else:
            payload = self._build(*key)
        finished = time.monotonic()
        with self._guard:
            previous = self._entries.get(key)
            self._entries[key] = _Entry(
                payload=payload,
                built_at=finished,
                build_seconds=finished - started,
                requested_at=previous.requested_at if previous else finished,
            )
        return payload
