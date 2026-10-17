"""Search across transcripts, summaries and episode titles."""

from fastapi import APIRouter, Depends, HTTPException, Query

from p3.api.auth import current_username
from p3.api.deps import get_db
from p3.api.models import SearchResult

router = APIRouter(prefix="/api/search", tags=["search"])

_SEGMENTS_PER_RESULT = 3
_SNIPPET_CONTEXT = 120


def _snippet(text: str, query: str) -> str:
    """The part of ``text`` around the first match of ``query``."""
    pos = text.lower().find(query.lower())
    if pos < 0:
        return text[: 2 * _SNIPPET_CONTEXT].strip()
    start = max(0, pos - _SNIPPET_CONTEXT)
    end = min(len(text), pos + len(query) + _SNIPPET_CONTEXT)
    snippet = " ".join(text[start:end].split())
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")


@router.get("", response_model=list[SearchResult])
def search(
    q: str = Query(...),
    limit: int = Query(50, ge=1, le=200),
    username: str = Depends(current_username),
):
    """Episodes in the user's library whose title, summary or transcript
    contains ``q`` (case-insensitive), with up to three matching transcript
    segments each."""
    query = q.strip()
    if len(query) < 2:
        raise HTTPException(400, "Search for at least 2 characters")

    results = []
    for row in get_db().search(query, limit, username):
        results.append(
            {
                "episode_id": row["id"],
                "episode_title": row["title"],
                "podcast_id": row["podcast_id"],
                "podcast_title": row["podcast_title"],
                "date": row["date"],
                "status": row["status"],
                "title_match": row["title_match"],
                "summary_snippet": (
                    _snippet(row["summary_text"], query)
                    if row["summary_match"]
                    else None
                ),
                "segment_count": row["segment_count"],
                "segments": row["segments"][:_SEGMENTS_PER_RESULT],
            }
        )
    return results
