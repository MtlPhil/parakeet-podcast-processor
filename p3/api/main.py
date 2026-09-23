"""FastAPI application for P³ web interface."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from p3.api.deps import close_db, get_db
from p3.api.job_queue import job_runner
from p3.api.models import StatsOut
from p3.api.routers import (
    blogs,
    episodes,
    exports,
    jobs,
    linkedin,
    podcasts,
    settings,
    summaries,
    transcripts,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    # Ensure directories exist
    for d in ("data", "data/audio", "exports", "blog_posts", "config"):
        Path(d).mkdir(parents=True, exist_ok=True)
    # Warm up database and clear jobs orphaned by a previous process
    db = get_db()
    stale = db.fail_stale_jobs()
    if stale:
        logger.warning("Marked %d interrupted job(s) as failed", stale)
    # Start the serial pipeline worker
    job_runner.start()
    logger.info("P3 API started")
    yield
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


@app.get("/api/stats", response_model=StatsOut, tags=["stats"])
def get_stats():
    """Dashboard statistics."""
    db = get_db()
    return db.get_stats()


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
