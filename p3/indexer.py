"""Build an episode's Ask index entry: its passages, their embeddings and
their BM25 terms (see p3/chunker.py and P3Database.replace_chunks)."""

import logging
from typing import Callable, Dict, List, Optional

from .chunker import build_chunks
from .database import P3Database
from .embedder import Embedder

logger = logging.getLogger(__name__)


def embedding_text(chunk: Dict) -> str:
    """What is embedded for a passage: its prefix, then its text."""
    return (
        f"{chunk['prefix']}\n{chunk['text']}" if chunk.get("prefix") else chunk["text"]
    )


def index_episode(db: P3Database, episode_id: int, embedder: Optional[Embedder]) -> int:
    """(Re)index one episode. Without an embedder the passages are stored
    for keyword search only. Returns the number of passages."""
    episode = db.get_episode_by_id(episode_id)
    if not episode:
        raise ValueError(f"Episode {episode_id} not found")
    segments = db.get_transcripts_for_episode(episode_id)
    summary = db.get_summary_by_episode(episode_id)
    chunks = build_chunks(episode, segments, summary)
    embeddings: Optional[List[List[float]]] = None
    if embedder is not None and chunks:
        embeddings = embedder.embed([embedding_text(c) for c in chunks])
    return db.replace_chunks(
        episode_id, chunks, embeddings, embedder.model if embedder else None
    )


def index_pending(
    db: P3Database,
    embedder: Optional[Embedder],
    progress: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, int]:
    """Index every processed episode not yet indexed with ``embedder``'s
    model. ``should_stop`` is checked between episodes, so a long backfill
    can give way. Returns counts of episodes indexed and failed."""
    pending = db.episodes_to_index(embedder.model if embedder else None)
    done = failed = 0
    for i, episode_id in enumerate(pending):
        if should_stop and should_stop():
            break
        if progress:
            progress(i, len(pending), f"Indexing episode {episode_id}")
        try:
            index_episode(db, episode_id, embedder)
            done += 1
        except Exception as e:
            logger.warning("Indexing episode %d failed: %s", episode_id, e)
            failed += 1
    return {"indexed": done, "failed": failed, "pending": len(pending)}
