"""Transcript viewing routes."""

from fastapi import APIRouter, HTTPException

from p3.api.deps import get_db
from p3.api.models import TranscriptSegment

router = APIRouter(prefix="/api/episodes", tags=["transcripts"])


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
