"""Search across transcripts, summaries and episode titles."""

from fastapi import APIRouter, Depends, HTTPException, Query

from p3.api.auth import current_username
from p3.api.deps import get_db
from p3.api.models import SearchResult

router = APIRouter(prefix="/api/search", tags=["search"])

_SEGMENTS_PER_RESULT = 3
_SNIPPET_CONTEXT = 120
# Passages of Ask's index ranked for a query (BM25) before keeping those
# with every one of its words.
_RANKED_PASSAGES = 300


def _snippet(text: str, query: str) -> str:
    """The part of ``text`` around the first match of ``query``."""
    pos = text.lower().find(query.lower())
    if pos < 0:
        return text[: 2 * _SNIPPET_CONTEXT].strip()
    start = max(0, pos - _SNIPPET_CONTEXT)
    end = min(len(text), pos + len(query) + _SNIPPET_CONTEXT)
    snippet = " ".join(text[start:end].split())
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")


def _ranked(db, query: str, username: str) -> dict:
    """{episode id: (best BM25 score, that passage)} over Ask's index, for
    passages holding every word of ``query`` as the index folds them (any
    accents, case or order, so "quebec" finds "Québec")."""
    from p3.chunker import tokenize

    terms = set(tokenize(query))
    if not terms:
        return {}
    hits = db.bm25_search(sorted(terms), username, None, _RANKED_PASSAGES)
    chunks = db.get_chunks([i for i, _ in hits])
    best: dict = {}
    for chunk_id, score in hits:
        chunk = chunks.get(chunk_id)
        if not chunk or not terms <= set(tokenize(chunk["text"])):
            continue
        if chunk["episode_id"] not in best:  # hits are best first
            best[chunk["episode_id"]] = (score, chunk)
    return best


def _passage_snippet(chunk: dict, query: str) -> dict:
    from p3.chunker import KIND_TRANSCRIPT

    text = _snippet(chunk["text"], query)
    if chunk["kind"] != KIND_TRANSCRIPT:
        return {"start": None, "text": text}
    return {"start": chunk.get("start_s"), "text": text}


@router.get("", response_model=list[SearchResult])
def search(
    q: str = Query(...),
    limit: int = Query(50, ge=1, le=200),
    username: str = Depends(current_username),
):
    """Episodes in the user's library whose title, summary or transcript
    contains ``q`` (case-insensitive), with up to three matching transcript
    segments each, plus those whose Ask passages hold all of its words in
    any form. Title matches come first, then by how well the best passage
    matches (BM25), then as ``P3Database.search`` orders them."""
    query = q.strip()
    if len(query) < 2:
        raise HTTPException(400, "Search for at least 2 characters")

    db = get_db()
    ranked = _ranked(db, query, username)
    rows = db.search(query, 200, username)
    found = {row["id"] for row in rows}
    for episode_id, (_, chunk) in ranked.items():
        if episode_id in found:
            continue
        episode = db.get_episode_by_id(episode_id)
        if episode:
            rows.append(
                {
                    **episode,
                    "title_match": False,
                    "summary_match": False,
                    "summary_text": None,
                    "segments": [_passage_snippet(chunk, query)],
                    "segment_count": 1,
                }
            )
    order = {row["id"]: n for n, row in enumerate(rows)}
    rows.sort(
        key=lambda r: (
            not r["title_match"],
            -ranked.get(r["id"], (0.0, None))[0],
            order[r["id"]],
        )
    )

    results = []
    for row in rows[:limit]:
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
