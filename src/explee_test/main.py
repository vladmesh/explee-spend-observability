import asyncio
import contextlib
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from explee_test import __version__
from explee_test.observability.alerts import (
    COLLECTOR_RULES,
    collector_alerts,
    open_alerts,
    recent_alerts,
    reconcile,
)
from explee_test.observability.cache import ProjectionCache
from explee_test.observability.dashboard import (
    build_overview,
    build_provider_detail,
    get_raw_response,
)
from explee_test.observability.store import initialise_database
from explee_test.settings import get_settings

WEB_ROOT = Path(__file__).with_name("web")

# Everything the page needs is served from this origin, so the policy can forbid
# third-party script entirely. Inline style is still allowed: the event strip and
# the sparklines position elements through style attributes, and a style attribute
# is a far smaller risk than a foreign script.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)
# Vendored files carry their version in the filename, so a new version is a new URL.
IMMUTABLE_PREFIX = "/static/vendor/"
# Everything else must be revalidated. Without an explicit policy a browser applies
# heuristic freshness and can keep running a previous deployment's JavaScript for
# hours; "no-cache" still allows a 304, so revalidation stays cheap.
REVALIDATE = "no-cache"
# The server configures its own logging and leaves the root logger without a
# handler, so anything this module has to say goes through the server's logger.
log = logging.getLogger("uvicorn.error")
WATCHDOG_INTERVAL_SECONDS = 30.0
REFRESH_INTERVAL_SECONDS = 5.0
# The windows the page offers. They are kept built, because the first viewer of a
# week should not be the one who pays for it.
PRESET_HOURS = (1, 6, 12, 24, 168)


# The database is part of the key, not read from settings inside the build: a
# projection of one capture must never be handed out as a projection of another.
def _overview(database: str, hours: int, start: str | None, end: str | None) -> dict:
    return build_overview(database, hours, start=start, end=end)


def _provider_detail(
    database: str, provider: str, hours: int, start: str | None, end: str | None
) -> dict:
    return build_provider_detail(database, provider, hours, start=start, end=end)


overview_cache = ProjectionCache(_overview)
# A drill-down is one provider out of fifteen in one of five windows, which is too
# many combinations to keep built; it is kept only while someone is reading it.
detail_cache = ProjectionCache(_provider_detail)


async def refresher() -> None:
    """Rebuild what is being looked at, off the request path.

    One projection per tick and in a worker thread: the host has a single core, and
    rebuilding everything that has come due in one go stalls every request for the
    sum of them. Spread out, the longest a request can wait behind the refresher is
    one projection, and the ones that come due most often are the cheap ones.
    """

    while True:
        for cache in (overview_cache, detail_cache):
            for key in cache.refresh_due()[:1]:
                # A key that cannot be built must not end the loop.
                with contextlib.suppress(Exception):
                    started = time.monotonic()
                    await asyncio.to_thread(cache.rebuild, key)
                    log.info(
                        "rebuilt %s in %.2fs", key, time.monotonic() - started
                    )
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)


async def watchdog() -> None:
    """Raise the one alert the collector cannot raise about itself.

    A dead poller writes nothing at all, alerts included. This process is separate
    and keeps reading the same database, which makes it the natural place to notice
    that the writing stopped. It only reconciles its own rule, so it never resolves
    an alert the collector owns.
    """

    settings = get_settings()
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
        try:
            now = datetime.now(UTC)
            reconcile(
                settings.database_path,
                settings.alerts_path,
                collector_alerts(settings.database_path, now),
                now,
                resolves=COLLECTOR_RULES,
            )
        except Exception:  # noqa: BLE001 - the read surface must stay up regardless
            pass


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    database_path = get_settings().database_path
    # The read surface must come up even if the schema step cannot get the write
    # lock: it reads, and the collector applies the same schema. Exiting here turned
    # a busy database into a crash loop that only luck got the page out of.
    try:
        initialise_database(database_path)
    except sqlite3.OperationalError:
        log.warning(
            "schema not applied at startup; serving reads and leaving it to the collector",
            exc_info=True,
        )
    overview_cache.keep_warm(
        tuple((str(database_path), hours, None, None) for hours in PRESET_HOURS)
    )
    tasks = [asyncio.create_task(watchdog()), asyncio.create_task(refresher())]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Explee Spend Observability", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")


@app.middleware("http")
async def security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.url.path.startswith(IMMUTABLE_PREFIX):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        response.headers.setdefault("Cache-Control", REVALIDATE)
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(WEB_ROOT / "dashboard.html")


@app.get("/api/overview")
def overview(
    hours: int = Query(default=12, ge=1, le=168),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> dict:
    try:
        return overview_cache.get((str(get_settings().database_path), hours, start, end))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid window") from exc


@app.get("/api/providers/{provider}")
def provider_detail(
    provider: str,
    hours: int = Query(default=12, ge=1, le=168),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> dict:
    try:
        return detail_cache.get(
            (str(get_settings().database_path), provider, hours, start, end)
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown provider") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid window") from exc


@app.get("/api/alerts")
def alerts(hours: int = Query(default=12, ge=1, le=168)) -> dict:
    """What is open now, worst first, and what came and went in the window."""

    path = get_settings().database_path
    return {"alerts": open_alerts(path), "recent": recent_alerts(path, hours)}


@app.get("/api/raw/{raw_response_id}")
def raw_response(raw_response_id: int) -> dict:
    """Evidence for one plotted point: the stored response it was derived from."""

    try:
        return get_raw_response(get_settings().database_path, raw_response_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown response") from exc
