"""Find the passages of a user's library that answer a question.

Keyword (BM25) and embedding search each return their top candidates; they
are merged by reciprocal rank fusion, then reordered by the cross-encoder
reranker when it is available. Each stage degrades on its own: without
Ollama the embedding search is skipped, without the reranker the fused order
stands, and the result says which stages ran.
"""

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .chunker import tokenize
from .database import P3Database
from .embedder import Embedder, Reranker
from .model_gate import ModelsPaused, model_use

logger = logging.getLogger(__name__)

RRF_K = 60
# Candidates from each search, and how many of the fused list are reranked.
CANDIDATES = 50
RERANK_CANDIDATES = 40
# Passages handed to the answer, and at most this many from one episode, so
# a single long episode can't crowd out the rest of the library.
TOP_K = 12
MAX_PER_EPISODE = 4


def reciprocal_rank_fusion(
    rankings: List[List[int]], k: int = RRF_K
) -> List[Tuple[int, float]]:
    """Merge ranked id lists: each id scores sum(1 / (k + rank)), rank from 1.
    Ties keep the order of first appearance."""
    scores: Dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    order = {item: i for i, item in enumerate(dict.fromkeys(sum(rankings, [])))}
    return sorted(scores.items(), key=lambda kv: (-kv[1], order[kv[0]]))


def limit_per_episode(
    ranked: List[Tuple[int, float]],
    episode_of: Dict[int, int],
    top_k: int = TOP_K,
    per_episode: int = MAX_PER_EPISODE,
) -> List[Tuple[int, float]]:
    """The first ``top_k`` of ``ranked`` with at most ``per_episode`` per
    episode."""
    kept: List[Tuple[int, float]] = []
    counts: Dict[int, int] = {}
    for chunk_id, score in ranked:
        episode = episode_of.get(chunk_id)
        if episode is None or counts.get(episode, 0) >= per_episode:
            continue
        counts[episode] = counts.get(episode, 0) + 1
        kept.append((chunk_id, score))
        if len(kept) >= top_k:
            break
    return kept


def clean_title(title: str, limit: int = 200) -> str:
    """A feed-supplied title on one line, capped: titles go into prompts
    outside the quoted passages, and any publisher controls them."""
    return " ".join(str(title or "").split())[:limit]


def passage_label(chunk: Dict[str, Any]) -> str:
    """How a passage is introduced to the reranker and the answer model."""
    date = chunk.get("episode_date")
    day = f" ({date:%Y-%m-%d})" if hasattr(date, "strftime") else ""
    return (
        f"{clean_title(chunk['podcast_title'])}: "
        f"{clean_title(chunk['episode_title'])}{day}"
    )


@dataclass
class Retrieval:
    """What the search found and how: ``passages`` (chunk dicts, best first,
    each with ``score`` and ``rerank_score``), every stage's ranking for
    debugging and evaluation, which stages were skipped, and timings."""

    passages: List[Dict[str, Any]] = field(default_factory=list)
    stages: Dict[str, List[Tuple[int, float]]] = field(default_factory=dict)
    skipped: Dict[str, str] = field(default_factory=dict)
    timings: Dict[str, float] = field(default_factory=dict)

    @property
    def best_rerank_score(self) -> Optional[float]:
        scores = [
            p["rerank_score"]
            for p in self.passages
            if p.get("rerank_score") is not None
        ]
        return max(scores) if scores else None

    def summary(self) -> Dict[str, Any]:
        """A JSON-friendly record of the retrieval, kept with each answer."""
        return {
            "stages": {
                k: [[i, round(s, 5)] for i, s in v] for k, v in self.stages.items()
            },
            "skipped": self.skipped,
            "passages": [p["id"] for p in self.passages],
        }


class Retriever:
    def __init__(
        self,
        db: P3Database,
        embedder: Optional[Embedder] = None,
        reranker: Optional[Reranker] = None,
    ):
        self.db = db
        self.embedder = embedder
        self.reranker = reranker

    def retrieve(
        self,
        question: str,
        username: Optional[str],
        keywords: Optional[List[str]] = None,
        filters: Optional[Dict[str, Any]] = None,
        top_k: int = TOP_K,
        use_models: bool = True,
        per_episode: int = MAX_PER_EPISODE,
    ) -> Retrieval:
        """Passages for ``question``. ``keywords`` (extra search terms, e.g.
        translations and names from the question step) widen the keyword
        search. ``use_models=False`` runs keyword search only, loading
        nothing (used while a transcription runs)."""
        result = Retrieval()
        started = time.monotonic()

        terms = tokenize(" ".join([question, *(keywords or [])]))
        bm25 = self.db.bm25_search(terms, username, filters, CANDIDATES)
        result.stages["bm25"] = bm25
        result.timings["bm25"] = time.monotonic() - started

        vector: List[Tuple[int, float]] = []
        if not use_models:
            result.skipped["vector"] = "models paused"
        elif self.embedder is None:
            result.skipped["vector"] = "no embedding model"
        else:
            t = time.monotonic()
            try:
                with model_use():
                    [query_vector] = self.embedder.embed([question])
                vector = self.db.vector_search(
                    query_vector, self.embedder.model, username, filters, CANDIDATES
                )
            except ModelsPaused:
                result.skipped["vector"] = "models paused"
            except Exception as e:  # Ollama down, model missing
                logger.warning("Embedding search skipped: %s", e)
                result.skipped["vector"] = str(e)
            result.timings["vector"] = time.monotonic() - t
        result.stages["vector"] = vector

        fused = reciprocal_rank_fusion([[i for i, _ in bm25], [i for i, _ in vector]])
        result.stages["fused"] = fused
        if not fused:
            result.timings["total"] = time.monotonic() - started
            return result

        candidates = fused[:RERANK_CANDIDATES]
        chunks = self.db.get_chunks([i for i, _ in candidates])
        ranked = candidates
        rerank_scores: Dict[int, float] = {}
        if not use_models:
            result.skipped["rerank"] = "models paused"
        elif self.reranker is None:
            result.skipped["rerank"] = "no reranker"
        else:
            t = time.monotonic()
            ids = [i for i, _ in candidates if i in chunks]
            try:
                with model_use():
                    scores = self.reranker.score(
                        question,
                        [
                            f"{passage_label(chunks[i])}\n{chunks[i]['text']}"
                            for i in ids
                        ],
                    )
                rerank_scores = dict(zip(ids, scores))
                ranked = sorted(rerank_scores.items(), key=lambda kv: -kv[1])
                result.stages["reranked"] = ranked
            except ModelsPaused:
                result.skipped["rerank"] = "models paused"
            except Exception as e:
                logger.warning("Reranking skipped: %s", e)
                result.skipped["rerank"] = str(e)
            result.timings["rerank"] = time.monotonic() - t

        # The same episode can be in the library twice (republished under a
        # new link): a passage whose words repeat one kept already is dropped.
        seen: set = set()
        unique = []
        for chunk_id, score in ranked:
            if chunk_id not in chunks:
                continue
            fingerprint = hashlib.sha1(
                " ".join(tokenize(chunks[chunk_id]["text"])).encode()
            ).hexdigest()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            unique.append((chunk_id, score))
        episode_of = {i: c["episode_id"] for i, c in chunks.items()}
        for chunk_id, score in limit_per_episode(
            unique, episode_of, top_k, per_episode
        ):
            passage = dict(chunks[chunk_id])
            passage["score"] = score
            passage["rerank_score"] = rerank_scores.get(chunk_id)
            result.passages.append(passage)
        result.timings["total"] = time.monotonic() - started
        return result


# Broad questions (overview, comparison, counting) look at episodes rather
# than single passages: up to this many episodes, each with its summary and
# its best passages.
BROAD_EPISODES = 8
BROAD_PASSAGES_PER_EPISODE = 2


def broaden(
    db: P3Database, retrieval: Retrieval, episodes: int = BROAD_EPISODES
) -> Retrieval:
    """Regroup a retrieval by episode for a broad question: the best
    ``episodes`` episodes in order of their best passage, each represented by
    its summary passage followed by its best passages."""
    order: List[int] = []
    best: Dict[int, List[Dict[str, Any]]] = {}
    for p in retrieval.passages:
        if p["episode_id"] not in best:
            order.append(p["episode_id"])
            best[p["episode_id"]] = []
        if (
            len(best[p["episode_id"]]) < BROAD_PASSAGES_PER_EPISODE
            and p["kind"] != "summary"
        ):
            best[p["episode_id"]].append(p)
    order = order[:episodes]
    summaries = db.summary_chunks(order)
    passages: List[Dict[str, Any]] = []
    for episode_id in order:
        top = max((p["score"] for p in best[episode_id]), default=0.0)
        summary = summaries.get(episode_id)
        if summary:
            passages.append({**summary, "score": top, "rerank_score": None})
        passages.extend(best[episode_id])
    retrieval.passages = passages
    retrieval.stages["episodes"] = [(e, 0.0) for e in order]
    return retrieval
