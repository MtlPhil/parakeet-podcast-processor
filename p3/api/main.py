"""FastAPI application for P³ web interface."""

import logging
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from p3.api.auth import current_username
from p3.api.deps import close_db, get_db
from p3.api.job_queue import job_runner
from p3.api.models import StatsOut
from p3.api.routers import (
    ask,
)
from p3.api.routers import auth as auth_routes
from p3.api.routers import (
    blogs,
    daily_run,
    episodes,
    exports,
    jobs,
    linkedin,
    podcasts,
    search,
    settings,
    setup,
    summaries,
    transcripts,
    users,
)
from p3.api.scheduler import daily_scheduler
from p3.api.tasks import resume_interrupted_runs


class _P3LogHandler(logging.StreamHandler):
    """The stderr handler configure_logging adds; its type marks it as
    already added."""


def configure_logging() -> None:
    """Send the app's own log messages (the ``p3.*`` loggers) to stderr at
    INFO, timestamped. Uvicorn only sets up its own loggers, so without this
    Python's fallback prints warnings and errors only: daily-run progress,
    "report emailed" and the like were lost. Level from ``P3_LOG_LEVEL``.
    Idempotent."""
    p3_logger = logging.getLogger("p3")
    if any(isinstance(h, _P3LogHandler) for h in p3_logger.handlers):
        return
    handler = _P3LogHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
        )
    )
    p3_logger.addHandler(handler)
    p3_logger.setLevel(os.environ.get("P3_LOG_LEVEL", "INFO").upper())


configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    # Ensure directories exist
    for d in (
        "data",
        "data/audio",
        "exports",
        "blog_posts",
        "linkedin_posts",
        "config",
    ):
        Path(d).mkdir(parents=True, exist_ok=True)
    # Warm up database and clear jobs orphaned by a previous process
    db = get_db()
    stale = db.fail_stale_jobs()
    # Questions a restart left without a reply get one saying so.
    closed = db.close_unanswered_ask_turns(
        "(The server restarted before this question was answered; ask it again.)"
    )
    if closed:
        logger.info("Closed %d Ask question(s) a restart left unanswered", closed)
    if stale:
        logger.warning("Marked %d interrupted job(s) as failed", stale)
    # Start the serial pipeline worker, then the daily run that feeds it
    job_runner.start()
    # A daily run the restart cut short is resumed, so its digest still goes
    # out. Runs older than two days are left alone.
    try:
        resumed = resume_interrupted_runs(db, max_age=timedelta(days=2))
        if resumed:
            logger.warning(
                "Resumed interrupted daily run(s) for: %s", ", ".join(resumed)
            )
    except Exception:
        logger.exception("Could not resume the interrupted daily run")
    daily_scheduler.start()
    logger.info("P3 API started")
    yield
    daily_scheduler.stop()
    job_runner.stop()
    close_db()
    logger.info("P3 API stopped")


app = FastAPI(
    title="Parakeet Podcast Processor",
    description="Web API for P³ podcast processing pipeline",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS: allow the Vite dev server during development.
DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=DEV_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.middleware("http")
async def reject_cross_site_writes(request: Request, call_next):
    """Refuse state-changing requests sent by other websites.

    CORS does not stop "simple" cross-origin POSTs from being executed, so a
    page open in the user's browser could otherwise start jobs or delete data
    on this unauthenticated local API. Browsers always send Origin on such
    requests; it must match this server's own host or the dev server.
    """
    origin = request.headers.get("origin")
    if request.method not in _SAFE_METHODS and origin:
        same_host = urlsplit(origin).netloc == request.headers.get("host")
        if not same_host and origin not in DEV_ORIGINS:
            return JSONResponse(
                {"detail": "Cross-origin request refused"}, status_code=403
            )
    return await call_next(request)


# Routers
app.include_router(podcasts.router)
app.include_router(episodes.router)
app.include_router(jobs.router)
app.include_router(transcripts.router)
app.include_router(summaries.router)
app.include_router(exports.router)
app.include_router(blogs.router)
app.include_router(linkedin.router)
app.include_router(settings.router)
app.include_router(search.router)
app.include_router(ask.router)
app.include_router(daily_run.router)
app.include_router(users.router)
app.include_router(auth_routes.router)
app.include_router(setup.router)


@app.get("/api/stats", response_model=StatsOut, tags=["stats"])
def get_stats(username: str = Depends(current_username)):
    """Dashboard statistics for the user's library."""
    db = get_db()
    return db.get_stats(username)


class SPAStaticFiles(StaticFiles):
    """Static files for a single-page app.

    Unknown page paths (e.g. /podcasts/3 after a browser refresh) are served
    index.html so the client-side router can render them. API paths and
    missing asset files (anything with a file extension) still return 404.
    """

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            is_api = path == "api" or path.startswith("api/")
            is_asset = "." in path.rsplit("/", 1)[-1]
            if exc.status_code != 404 or is_api or is_asset:
                raise
            return await super().get_response("index.html", scope)


# Serve the React frontend build (production)
FRONTEND_BUILD = Path("frontend/dist")
if FRONTEND_BUILD.exists():
    app.mount(
        "/", SPAStaticFiles(directory=str(FRONTEND_BUILD), html=True), name="frontend"
    )
