"""Summary viewing routes.

Summaries carry both languages: the English fields and their ``*_fr``
(Quebec French) counterparts; the frontend picks per the user's language.
"""

from fastapi import APIRouter, Depends, HTTPException

from p3.api.auth import current_username
from p3.api.deps import get_db
from p3.api.models import SummaryOut
from p3.api.routers.episodes import user_episode

router = APIRouter(prefix="/api", tags=["summaries"])


@router.get("/episodes/{episode_id}/summary", response_model=SummaryOut)
def get_episode_summary(episode_id: int, username: str = Depends(current_username)):
    db = get_db()
    user_episode(db, episode_id, username)

    summary = db.get_summary_by_episode(episode_id)
    if not summary:
        raise HTTPException(404, "No summary found for this episode")
    return summary


@router.get("/summaries", response_model=list[SummaryOut])
def list_summaries(username: str = Depends(current_username)):
    return get_db().get_all_summaries(username)
