"""Transcript viewing routes."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from p3.api.deps import get_db
from p3.api.models import TranscriptSegment
from p3.exporter import DigestExporter

router = APIRouter(prefix="/api/episodes", tags=["transcripts"])


@router.post("/transcripts/dedupe", response_model=dict)
def dedupe_transcripts():
    """Remove duplicate transcript segments from databases created before
    transcript writes became idempotent. Declared before
    ``/{episode_id}/transcript`` so ``transcripts`` is not read as an id."""
    db = get_db()
    return {"deleted": db.dedupe_transcripts()}


@router.get("/{episode_id}/transcript", response_model=list[TranscriptSegment])
def get_transcript(episode_id: int):
    db = get_db()
    episode = db.get_episode_by_id(episode_id)
    if not episode:
        raise HTTPException(404, "Episode not found")

    segments = db.get_transcripts_for_episode(episode_id)
    if not segments:
        raise HTTPException(404, "No transcript found for this episode")
    return segments


@router.get("/{episode_id}/transcript/export")
def export_transcript(episode_id: int):
    """Download this episode's transcript as a markdown file of sustained
    prose (no per-segment timestamps or line breaks)."""
    db = get_db()
    episode = db.get_episode_by_id(episode_id)
    if not episode:
        raise HTTPException(404, "Episode not found")

    segments = db.get_transcripts_for_episode(episode_id)
    if not segments:
        raise HTTPException(404, "No transcript found for this episode")

    from p3.downloader import _safe_filename

    content = DigestExporter(db).export_transcript_markdown(episode, segments)
    filename = f"{_safe_filename(episode['podcast_title'])}-{_safe_filename(episode['title'])}.md"
    return Response(
        content=content,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
