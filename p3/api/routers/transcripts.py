"""Transcript viewing routes."""

from fastapi import APIRouter, HTTPException

from p3.api.deps import get_db
from p3.api.models import TranscriptSegment

router = APIRouter(prefix="/api/episodes", tags=["transcripts"])


@router.post("/transcripts/dedupe", response_model=dict)
def dedupe_transcripts():
    """Remove duplicate transcript segments left by an episode that was
    fully transcribed more than once (an interrupted prior attempt that
    never flipped the episode's status, so it was picked up and
    retranscribed). Declared before ``/{episode_id}/transcript`` so
    ``transcripts`` is not read as an id."""
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
