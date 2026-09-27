"""Podcast CRUD routes."""

import io
import zipfile
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from p3.api.deps import get_db
from p3.api.job_queue import job_runner
from p3.api.models import (
    FetchAction,
    PodcastCreate,
    PodcastOut,
    PodcastUpdate,
    SourcePreview,
)
from p3.api.tasks import queue_step_jobs, task_fetch, task_import_playlist

router = APIRouter(prefix="/api/podcasts", tags=["podcasts"])


@router.get("", response_model=list[PodcastOut])
def list_podcasts():
    db = get_db()
    podcasts = db.get_all_podcasts()
    # Attach episode count
    for p in podcasts:
        eps = db.get_episodes_by_podcast(p["id"])
        p["episode_count"] = len(eps)
    return podcasts


@router.get("/preview", response_model=SourcePreview)
def preview_source(url: str):
    """List every available episode for a source URL, to hand-pick which
    ones to fetch initially.

    RSS feeds return every entry; YouTube channels are limited to uploads
    from the last 12 months (see ``PodcastDownloader.list_preview_episodes``).
    A playlist can't be previewed: it is imported as one source per video
    rather than picked from.
    """
    db = get_db()

    from p3 import youtube
    from p3.downloader import PodcastDownloader
    from p3.url_resolver import is_youtube_playlist, resolve_source

    if is_youtube_playlist(url):
        raise HTTPException(400, "Playlists import every video automatically")

    try:
        source = resolve_source(url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except youtube.YouTubeError as e:
        raise HTTPException(502, f"YouTube lookup failed: {e}")

    if db.get_podcast_by_url(source.url):
        raise HTTPException(409, "Podcast with this URL already exists")

    downloader = PodcastDownloader(db=db)
    try:
        episodes = downloader.list_preview_episodes(source.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except youtube.YouTubeError as e:
        raise HTTPException(502, f"YouTube lookup failed: {e}")

    _epoch = datetime.min.replace(tzinfo=timezone.utc)
    episodes.sort(key=lambda e: e["date"] or _epoch, reverse=True)
    return {
        "url": source.url,
        "name": source.name,
        "source_type": source.source_type,
        "episodes": [
            {
                "guid": e["guid"],
                "title": e["title"],
                "date": e["date"],
                "description": e["description"],
            }
            for e in episodes
        ],
    }


@router.get("/{podcast_id}", response_model=PodcastOut)
def get_podcast(podcast_id: int):
    db = get_db()
    podcast = db.get_podcast_by_id(podcast_id)
    if not podcast:
        raise HTTPException(404, "Podcast not found")
    eps = db.get_episodes_by_podcast(podcast_id)
    podcast["episode_count"] = len(eps)
    return podcast


@router.post("", response_model=dict)
def add_podcast(body: PodcastCreate):
    """Add a source and start fetching it.

    Accepts an RSS feed, an Apple Podcasts link, or a YouTube channel or
    video. A YouTube playlist is not stored as a source: it queues one import
    job that adds each video as its own source, and the response carries
    that job's id without a ``podcast_id``.
    """
    db = get_db()

    from p3 import youtube
    from p3.url_resolver import is_youtube_playlist, resolve_source

    if is_youtube_playlist(body.url):
        playlist_url = youtube.parse_youtube_url(body.url).url
        job_id = db.create_job("import_playlist")
        job_runner.enqueue(task_import_playlist, job_id, playlist_url, body.category)
        return {"podcast_id": None, "job_id": job_id}

    try:
        source = resolve_source(body.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except youtube.YouTubeError as e:
        raise HTTPException(502, f"YouTube lookup failed: {e}")

    # Check if already exists
    if db.get_podcast_by_url(source.url):
        raise HTTPException(409, "Podcast with this URL already exists")
    # A video already fetched as an episode of a channel stays there.
    if source.source_type == youtube.SOURCE_VIDEO and db.episode_exists(source.url):
        raise HTTPException(409, "This video is already in the library")

    # Use provided name, resolved name from lookup, or derive from URL
    name = body.name or source.name or source.url.split("/")[-1] or "Untitled Podcast"
    podcast_id = db.add_podcast(name, source.url, body.category, source.source_type)

    # Queue the initial fetch. If episode_guids was hand-picked from a
    # preview listing, only those are downloaded instead of the top-N.
    job_id = db.create_job("fetch", podcast_id=podcast_id)
    job_runner.enqueue(task_fetch, job_id, podcast_id, None, body.episode_guids)

    return {"podcast_id": podcast_id, "job_id": job_id}


@router.post("/{podcast_id}/fetch", response_model=dict)
def fetch_podcast(podcast_id: int, body: Optional[FetchAction] = None):
    """Trigger a new fetch for an existing podcast."""
    db = get_db()
    podcast = db.get_podcast_by_id(podcast_id)
    if not podcast:
        raise HTTPException(404, "Podcast not found")

    max_eps = body.max_episodes if body else None
    job_id = db.create_job("fetch", podcast_id=podcast_id)
    job_runner.enqueue(task_fetch, job_id, podcast_id, max_eps)
    return {"job_id": job_id}


@router.post("/{podcast_id}/process/{step}", response_model=dict)
def process_podcast_episodes(podcast_id: int, step: str):
    """Queue a pipeline step (transcribe|digest|pipeline) for every eligible
    episode of this podcast. One job per episode; the runner executes them one
    at a time."""
    db = get_db()
    if not db.get_podcast_by_id(podcast_id):
        raise HTTPException(404, "Podcast not found")
    try:
        job_ids = queue_step_jobs(db, db.get_episodes_by_podcast(podcast_id), step)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"queued": len(job_ids), "job_ids": job_ids}


@router.post("/{podcast_id}/transcripts/export")
def export_podcast_transcripts(podcast_id: int):
    """Download every transcribed episode's transcript as a separate
    markdown file, bundled into one zip archive."""
    db = get_db()
    podcast = db.get_podcast_by_id(podcast_id)
    if not podcast:
        raise HTTPException(404, "Podcast not found")

    from p3.downloader import _safe_filename
    from p3.exporter import DigestExporter

    exporter = DigestExporter(db)
    buffer = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for episode in db.get_episodes_by_podcast(podcast_id):
            segments = db.get_transcripts_for_episode(episode["id"])
            if not segments:
                continue
            content = exporter.export_transcript_markdown(episode, segments)
            zf.writestr(
                f"{episode['id']}_{_safe_filename(episode['title'])}.md", content
            )
            count += 1

    if count == 0:
        raise HTTPException(404, "No transcripts available for this podcast")

    filename = f"{_safe_filename(podcast['title'])}.zip"
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.patch("/{podcast_id}", response_model=PodcastOut)
def update_podcast(podcast_id: int, body: PodcastUpdate):
    """Update a podcast's title or category.

    The RSS URL is intentionally not editable: it is a UNIQUE column, and
    DuckDB cannot update an indexed column on a row that is referenced by a
    foreign key (episodes.podcast_id) once the podcast has episodes.
    """
    db = get_db()
    podcast = db.get_podcast_by_id(podcast_id)
    if not podcast:
        raise HTTPException(404, "Podcast not found")

    db.update_podcast(podcast_id, title=body.title, category=body.category)
    updated = db.get_podcast_by_id(podcast_id)
    if not updated:
        raise HTTPException(404, "Podcast not found")
    updated["episode_count"] = len(db.get_episodes_by_podcast(podcast_id))
    return updated


@router.delete("/{podcast_id}")
def delete_podcast(podcast_id: int):
    db = get_db()
    podcast = db.get_podcast_by_id(podcast_id)
    if not podcast:
        raise HTTPException(404, "Podcast not found")
    db.delete_podcast(podcast_id)
    return {"deleted": True}
