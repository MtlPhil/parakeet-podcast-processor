"""Podcast CRUD routes.

A podcast row is shared by everyone who follows it; each user sees only the
podcasts they subscribe to (see the users tables in P3Database).
"""

import io
import zipfile
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from p3.api.auth import current_username
from p3.api.deps import get_db
from p3.api.job_queue import job_runner
from p3.api.models import (
    BulkPodcastCreate,
    FetchAction,
    PodcastCreate,
    PodcastOut,
    PodcastUpdate,
    SourcePreview,
)
from p3.api.tasks import (
    queue_step_jobs,
    task_bulk_add,
    task_fetch,
    task_import_playlist,
)

router = APIRouter(prefix="/api/podcasts", tags=["podcasts"])


def _subscribed_podcast(db, podcast_id: int, username: str) -> dict:
    """The podcast, or 404 if it isn't in the user's library."""
    podcast = db.get_podcast_by_id(podcast_id)
    if not podcast or not db.is_subscribed(username, podcast_id):
        raise HTTPException(404, "Podcast not found")
    return podcast


@router.get("", response_model=list[PodcastOut])
def list_podcasts(username: str = Depends(current_username)):
    db = get_db()
    podcasts = db.get_all_podcasts(username)
    for p in podcasts:
        p["episode_count"] = len(db.get_episodes_by_podcast(p["id"], username))
    return podcasts


@router.get("/preview", response_model=SourcePreview)
def preview_source(url: str, username: str = Depends(current_username)):
    """List every available episode for a source URL, to hand-pick which
    ones to fetch initially.

    RSS feeds return every entry; YouTube channels are limited to uploads
    from the last 12 months (see ``PodcastDownloader.list_preview_episodes``).
    A playlist can't be previewed: it is imported as one source per video
    rather than picked from. A source someone else already follows can be
    previewed and added; one already in the user's library is a 409.
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

    existing = db.get_podcast_by_url(source.url)
    if existing and db.is_subscribed(username, existing["id"]):
        raise HTTPException(409, "This podcast is already in your library")

    downloader = PodcastDownloader(db=db)
    try:
        episodes = downloader.list_preview_episodes(source.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except youtube.YouTubeError as e:
        raise HTTPException(502, f"YouTube lookup failed: {e}")

    return {
        "url": source.url,
        "name": source.name,
        "source_type": source.source_type,
        "episodes": _preview_episodes(db, episodes, username),
    }


@router.get("/{podcast_id}", response_model=PodcastOut)
def get_podcast(podcast_id: int, username: str = Depends(current_username)):
    db = get_db()
    podcast = _subscribed_podcast(db, podcast_id, username)
    podcast["episode_count"] = len(db.get_episodes_by_podcast(podcast_id, username))
    return podcast


@router.get("/{podcast_id}/available", response_model=SourcePreview)
def list_available_episodes(podcast_id: int, username: str = Depends(current_username)):
    """Every episode the source offers, to hand-pick past ones to add.
    Episodes already in the user's library are flagged ``in_library``."""
    db = get_db()
    podcast = _subscribed_podcast(db, podcast_id, username)

    from p3 import youtube
    from p3.downloader import PodcastDownloader

    try:
        episodes = PodcastDownloader(db=db).list_preview_episodes(podcast["rss_url"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    except youtube.YouTubeError as e:
        raise HTTPException(502, f"YouTube lookup failed: {e}")

    return {
        "url": podcast["rss_url"],
        "name": podcast["title"],
        "source_type": podcast["source_type"],
        "episodes": _preview_episodes(db, episodes, username),
    }


def _preview_episodes(db, episodes: list[dict], username: str) -> list[dict]:
    """Preview listing rows, newest first, flagged if already in the user's
    library. An episode someone else has, or that the user deleted, is not
    flagged: picking it links it without downloading it again."""
    _epoch = datetime.min.replace(tzinfo=timezone.utc)
    episodes.sort(key=lambda e: e["date"] or _epoch, reverse=True)
    rows = []
    for e in episodes:
        episode_id = db.get_episode_id_by_url(e["url"])
        rows.append(
            {
                "guid": e["guid"],
                "title": e["title"],
                "date": e["date"],
                "description": e["description"],
                "in_library": episode_id is not None
                and db.user_has_episode(username, episode_id),
            }
        )
    return rows


@router.post("", response_model=dict)
def add_podcast(body: PodcastCreate, username: str = Depends(current_username)):
    """Add a source to the user's library and start fetching it.

    Accepts an RSS feed, an Apple Podcasts link, or a YouTube channel or
    video. A source someone else already follows is subscribed to rather
    than added again. A YouTube playlist is not stored as a source: it
    queues one import job that adds each video as its own source, and the
    response carries that job's id without a ``podcast_id``.
    """
    db = get_db()

    from p3 import youtube
    from p3.url_resolver import is_youtube_playlist, resolve_source

    if is_youtube_playlist(body.url):
        playlist_url = youtube.parse_youtube_url(body.url).url
        job_id = db.create_job("import_playlist", username=username)
        job_runner.enqueue(
            task_import_playlist, job_id, playlist_url, body.category, username
        )
        return {"podcast_id": None, "job_id": job_id}

    try:
        source = resolve_source(body.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except youtube.YouTubeError as e:
        raise HTTPException(502, f"YouTube lookup failed: {e}")

    existing = db.get_podcast_by_url(source.url)
    if existing and db.is_subscribed(username, existing["id"]):
        raise HTTPException(409, "This podcast is already in your library")
    # A video already fetched as an episode of a channel stays there.
    if (
        not existing
        and source.source_type == youtube.SOURCE_VIDEO
        and db.episode_exists(source.url)
    ):
        raise HTTPException(409, "This video is already an episode of a podcast")

    if existing:
        podcast_id = existing["id"]
    else:
        # Use provided name, resolved name from lookup, or derive from URL
        name = (
            body.name or source.name or source.url.split("/")[-1] or "Untitled Podcast"
        )
        podcast_id = db.add_podcast(name, source.url, body.category, source.source_type)
    db.subscribe(username, podcast_id)

    # Queue the initial fetch. If episode_guids was hand-picked from a
    # preview listing, only those are downloaded instead of the top-N.
    job_id = db.create_job(
        "fetch",
        podcast_id=podcast_id,
        username=username,
        episode_guids=body.episode_guids,
    )
    job_runner.enqueue(
        task_fetch, job_id, podcast_id, None, body.episode_guids, username
    )

    return {"podcast_id": podcast_id, "job_id": job_id}


@router.post("/bulk", response_model=dict)
def bulk_add_podcasts(
    body: BulkPodcastCreate, username: str = Depends(current_username)
):
    """Add many sources at once. One job resolves every URL, adds the new
    ones and queues a fetch of the top episodes for each."""
    urls = list(dict.fromkeys(u.strip() for u in body.urls if u.strip()))
    if not urls:
        raise HTTPException(400, "No URLs given")
    db = get_db()
    job_id = db.create_job("bulk_add", username=username)
    job_runner.enqueue(task_bulk_add, job_id, urls, body.category, username)
    return {"job_id": job_id, "count": len(urls)}


@router.post("/{podcast_id}/fetch", response_model=dict)
def fetch_podcast(
    podcast_id: int,
    body: Optional[FetchAction] = None,
    username: str = Depends(current_username),
):
    """Trigger a new fetch for a podcast in the user's library: the newest
    episodes per their settings, or the hand-picked ``episode_guids``."""
    db = get_db()
    _subscribed_podcast(db, podcast_id, username)

    max_eps = body.max_episodes if body else None
    guids = body.episode_guids if body else None
    job_id = db.create_job(
        "fetch",
        podcast_id=podcast_id,
        username=username,
        episode_guids=guids,
        max_episodes=max_eps,
    )
    job_runner.enqueue(task_fetch, job_id, podcast_id, max_eps, guids, username)
    return {"job_id": job_id}


@router.post("/{podcast_id}/process/{step}", response_model=dict)
def process_podcast_episodes(
    podcast_id: int, step: str, username: str = Depends(current_username)
):
    """Queue a pipeline step (transcribe|digest|pipeline) for every eligible
    episode of this podcast in the user's library. One job per episode; the
    runner executes them one at a time."""
    db = get_db()
    _subscribed_podcast(db, podcast_id, username)
    try:
        job_ids = queue_step_jobs(
            db,
            db.get_episodes_by_podcast(podcast_id, username),
            step,
            username=username,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"queued": len(job_ids), "job_ids": job_ids}


@router.post("/{podcast_id}/transcripts/export")
def export_podcast_transcripts(
    podcast_id: int, username: str = Depends(current_username)
):
    """Download every transcribed episode's transcript as a separate
    markdown file, bundled into one zip archive."""
    db = get_db()
    podcast = _subscribed_podcast(db, podcast_id, username)

    from p3.downloader import _safe_filename
    from p3.exporter import DigestExporter

    exporter = DigestExporter(db)
    buffer = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for episode in db.get_episodes_by_podcast(podcast_id, username):
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
def update_podcast(
    podcast_id: int, body: PodcastUpdate, username: str = Depends(current_username)
):
    """Update a podcast's title or category (shared by everyone who follows
    it).

    The RSS URL is intentionally not editable: it is a UNIQUE column, and
    DuckDB cannot update an indexed column on a row that is referenced by a
    foreign key (episodes.podcast_id) once the podcast has episodes.
    """
    db = get_db()
    _subscribed_podcast(db, podcast_id, username)

    db.update_podcast(podcast_id, title=body.title, category=body.category)
    updated = db.get_podcast_by_id(podcast_id)
    if not updated:
        raise HTTPException(404, "Podcast not found")
    updated["episode_count"] = len(db.get_episodes_by_podcast(podcast_id, username))
    return updated


@router.delete("/{podcast_id}")
def delete_podcast(podcast_id: int, username: str = Depends(current_username)):
    """Remove a podcast from the user's library. Once nobody follows it, it
    is deleted with all its episodes, transcripts, summaries and audio."""
    db = get_db()
    _subscribed_podcast(db, podcast_id, username)
    if db.unsubscribe(username, podcast_id) == 0:
        for episode in db.get_episodes_by_podcast(podcast_id):
            db.remove_audio(episode["id"])
        db.delete_podcast(podcast_id)
    return {"deleted": True}
