"""Evaluate Ask: generate a question set from the library, score retrieval
stage by stage, and compare two runs with a paired bootstrap.

The question set and results are derived from the library, so they live in
gitignored ``data/eval/``. A question is a dict::

    {"id": "q001", "question": "...", "language": "en" | "fr",
     "stratum": "lookup" | "no_answer" | ...,
     "relevant": [{"episode_id": 12, "start_s": 300.0, "end_s": 360.0}],
     "reference": "the expected answer, if known"}

A passage counts as relevant when it is from a listed episode and, if the
entry has a time range, overlaps it (within ``SLACK_SECONDS``). Run from the
repo root (the server must be stopped, or point --db at a copy)::

    python -m p3.eval_ask generate --db data/p3.duckdb --count 60
    python -m p3.eval_ask retrieval --db data/p3.duckdb
"""

import argparse
import json
import logging
import random
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .cleaner import _extract_json
from .database import P3Database
from .llm import LLMClient
from .retriever import Retriever

logger = logging.getLogger(__name__)

EVAL_DIR = Path("data/eval")
QUESTIONS_FILE = EVAL_DIR / "questions.json"
SLACK_SECONDS = 30.0
K = 12

_GENERATE_SYSTEM = """You write test questions for a search engine over \
podcast transcripts. Given one transcript passage, write ONE question that a \
curious listener might ask and that this passage answers specifically (a \
fact, a claim, a number, a name, an opinion). The question must make sense \
without seeing the passage: name the subject, never say "this passage" or \
"the speaker". Write it in the passage's language. Reply with JSON only:
{"question": "...", "answer": "the answer, in one sentence, from the passage"}
If the passage is small talk, ads or has no answerable content, reply {"question": null}."""


def is_relevant(passage: Dict[str, Any], item: Dict[str, Any]) -> bool:
    for target in item.get("relevant") or []:
        if passage["episode_id"] != target["episode_id"]:
            continue
        if target.get("start_s") is None:
            return True  # any passage of the episode
        if passage.get("start_s") is None:
            continue  # a time range needs a transcript passage
        if (
            passage["start_s"] <= target["end_s"] + SLACK_SECONDS
            and passage["end_s"] >= target["start_s"] - SLACK_SECONDS
        ):
            return True
    return False


def first_relevant_rank(
    ranking: Sequence[int], chunks: Dict[int, Dict[str, Any]], item: Dict[str, Any]
) -> Optional[int]:
    """1-based rank of the first relevant passage in ``ranking``, or None."""
    for rank, chunk_id in enumerate(ranking, start=1):
        chunk = chunks.get(chunk_id)
        if chunk and is_relevant(chunk, item):
            return rank
    return None


def summarize(ranks: List[Optional[int]], k: int = K) -> Dict[str, float]:
    """recall@k and MRR (reciprocal rank, 0 when not found) over ranks."""
    if not ranks:
        return {"n": 0, "recall": 0.0, "mrr": 0.0}
    return {
        "n": len(ranks),
        "recall": sum(1 for r in ranks if r is not None and r <= k) / len(ranks),
        "mrr": sum(1 / r for r in ranks if r is not None) / len(ranks),
    }


def paired_bootstrap(
    a: Sequence[float], b: Sequence[float], samples: int = 2000, seed: int = 0
) -> Dict[str, float]:
    """Mean of b - a over paired per-question scores, with a 95% interval
    from resampling questions. An interval that excludes 0 means b really
    differs from a on this question set."""
    if len(a) != len(b) or not a:
        raise ValueError("Paired scores must be non-empty and the same length")
    diffs = [y - x for x, y in zip(a, b)]
    rng = random.Random(seed)
    means = sorted(
        statistics.fmean(rng.choice(diffs) for _ in diffs) for _ in range(samples)
    )
    return {
        "mean_diff": statistics.fmean(diffs),
        "low": means[int(0.025 * samples)],
        "high": means[int(0.975 * samples) - 1],
    }


def load_questions(path: Path = QUESTIONS_FILE) -> List[Dict[str, Any]]:
    return json.loads(path.read_text())


def generate_questions(
    db: P3Database, llm: LLMClient, count: int, seed: int = 1
) -> List[Dict[str, Any]]:
    """``count`` lookup questions, each written from a random substantial
    transcript passage, spread over episodes. To be reviewed by hand: drop
    the poor ones, add real questions and other strata."""
    rows = db.conn.execute(
        "SELECT id, episode_id, start_s, end_s, text, language FROM chunks "
        "WHERE kind = 'transcript' AND length(text) > 500 ORDER BY id"
    ).fetchall()
    rng = random.Random(seed)
    rng.shuffle(rows)
    per_episode: Dict[int, int] = {}
    questions: List[Dict[str, Any]] = []
    for chunk_id, episode_id, start_s, end_s, text, language in rows:
        if len(questions) >= count:
            break
        if per_episode.get(episode_id, 0) >= 2:
            continue
        try:
            data = _extract_json(
                llm.chat(_GENERATE_SYSTEM, text[:3000], max_tokens=300)
            )
        except Exception as e:
            logger.warning("Question generation failed for chunk %d: %s", chunk_id, e)
            continue
        if not data or not data.get("question"):
            continue
        per_episode[episode_id] = per_episode.get(episode_id, 0) + 1
        questions.append(
            {
                "id": f"q{len(questions) + 1:03d}",
                "question": str(data["question"]).strip(),
                "language": language or "en",
                "stratum": "lookup",
                "relevant": [
                    {"episode_id": episode_id, "start_s": start_s, "end_s": end_s}
                ],
                "reference": str(data.get("answer") or "").strip(),
                "source_chunk": chunk_id,
                "generated": True,
            }
        )
    return questions


def evaluate_retrieval(
    db: P3Database,
    retriever: Retriever,
    questions: List[Dict[str, Any]],
    username: Optional[str] = None,
    k: int = K,
) -> Dict[str, Any]:
    """Rank of the first relevant passage for every question at every stage
    (bm25, vector, fused, reranked, final), with recall@k and MRR overall
    and per stratum. No-answer questions are reported by their best
    reranker score instead (for calibrating the abstain threshold)."""
    per_question = []
    for item in questions:
        started = time.monotonic()
        result = retriever.retrieve(item["question"], username)
        elapsed = time.monotonic() - started
        ids = {i for ranking in result.stages.values() for i, _ in ranking}
        chunks = db.get_chunks(sorted(ids))
        ranks = {
            stage: first_relevant_rank([i for i, _ in ranking], chunks, item)
            for stage, ranking in result.stages.items()
        }
        ranks["final"] = first_relevant_rank(
            [p["id"] for p in result.passages], chunks, item
        )
        per_question.append(
            {
                "id": item["id"],
                "stratum": item.get("stratum", "lookup"),
                "language": item.get("language"),
                "ranks": ranks,
                "best_rerank_score": result.best_rerank_score,
                "seconds": round(elapsed, 3),
            }
        )

    def table(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        stages = sorted({s for r in rows for s in r["ranks"]})
        return {s: summarize([r["ranks"].get(s) for r in rows], k) for s in stages}

    answerable = [r for r in per_question if r["stratum"] != "no_answer"]
    strata: Dict[str, List[Dict[str, Any]]] = {}
    for r in answerable:
        strata.setdefault(r["stratum"], []).append(r)
        strata.setdefault(f"lang:{r['language']}", []).append(r)
    return {
        "k": k,
        "overall": table(answerable),
        "strata": {name: table(rows) for name, rows in sorted(strata.items())},
        "abstain": {
            "answerable_scores": sorted(
                r["best_rerank_score"]
                for r in answerable
                if r["best_rerank_score"] is not None
            ),
            "no_answer_scores": sorted(
                r["best_rerank_score"]
                for r in per_question
                if r["stratum"] == "no_answer" and r["best_rerank_score"] is not None
            ),
        },
        "latency": {
            "median": (
                statistics.median([r["seconds"] for r in per_question])
                if per_question
                else None
            ),
            "max": max((r["seconds"] for r in per_question), default=None),
        },
        "questions": per_question,
    }


def reciprocal_ranks(result: Dict[str, Any], stage: str = "final") -> Dict[str, float]:
    """Per-question 1/rank at ``stage`` (0 when not found), for comparisons."""
    return {
        q["id"]: (1 / q["ranks"][stage] if q["ranks"].get(stage) else 0.0)
        for q in result["questions"]
        if q["stratum"] != "no_answer"
    }


def compare(
    before: Dict[str, Any], after: Dict[str, Any], stage: str = "final"
) -> Dict[str, float]:
    """Paired bootstrap of MRR at ``stage`` between two retrieval results on
    the same questions."""
    a, b = reciprocal_ranks(before, stage), reciprocal_ranks(after, stage)
    shared = sorted(set(a) & set(b))
    return paired_bootstrap([a[q] for q in shared], [b[q] for q in shared])


def _retriever(db: P3Database, rerank: bool) -> Retriever:
    from .embedder import Embedder, get_reranker

    return Retriever(db, Embedder(), get_reranker() if rerank else None)


def main(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=["generate", "retrieval", "compare"])
    parser.add_argument("--db", default="data/p3.duckdb")
    parser.add_argument("--questions", default=str(QUESTIONS_FILE))
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--model", default=None, help="Ollama model for generation")
    parser.add_argument("--no-rerank", action="store_true")
    parser.add_argument("--out", default=None)
    parser.add_argument("files", nargs="*", help="compare: two result files")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.command == "compare":
        before, after = (json.loads(Path(f).read_text()) for f in args.files)
        print(json.dumps(compare(before, after), indent=2))
        return

    db = P3Database(args.db)
    try:
        if args.command == "generate":
            from .llm import DEFAULT_OLLAMA_MODEL

            llm = LLMClient("ollama", args.model or DEFAULT_OLLAMA_MODEL)
            questions = generate_questions(db, llm, args.count)
            out = Path(args.out or args.questions)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(questions, indent=2, ensure_ascii=False))
            print(f"Wrote {len(questions)} questions to {out}")
        else:
            result = evaluate_retrieval(
                db,
                _retriever(db, not args.no_rerank),
                load_questions(Path(args.questions)),
            )
            out = Path(
                args.out
                or EVAL_DIR / "results" / f"retrieval-{datetime.now():%Y%m%d-%H%M}.json"
            )
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(result, indent=2, default=str))
            print(
                json.dumps(
                    {"overall": result["overall"], "latency": result["latency"]},
                    indent=2,
                )
            )
            print(f"Full results: {out}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
