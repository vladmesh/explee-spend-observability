import asyncio
import contextlib
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
WATCHDOG_INTERVAL_SECONDS = 30.0


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
    initialise_database(database_path)
    task = asyncio.create_task(watchdog())
    try:
        yield
    finally:
        task.cancel()
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
        return build_overview(get_settings().database_path, hours, start=start, end=end)
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
        return build_provider_detail(
            get_settings().database_path, provider, hours, start=start, end=end
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
