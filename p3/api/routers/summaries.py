"""Summary viewing routes."""

from fastapi import APIRouter, HTTPException

from p3.api.deps import get_db
from p3.api.models import SummaryOut

router = APIRouter(prefix="/api", tags=["summaries"])


@router.get("/episodes/{episode_id}/summary", response_model=SummaryOut)
def get_episode_summary(episode_id: int):
    db = get_db()
    episode = db.get_episode_by_id(episode_id)
    if not episode:
        raise HTTPException(404, "Episode not found")

    summary = db.get_summary_by_episode(episode_id)
    if not summary:
        raise HTTPException(404, "No summary found for this episode")
    return summary


@router.get("/summaries", response_model=list[SummaryOut])
def list_summaries():
    db = get_db()
    return db.get_all_summaries()
