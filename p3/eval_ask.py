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
    python -m p3.eval_ask answers --db data/p3.duckdb   # slow: asks every question
"""

import argparse
import json
import logging
import os
import random
import statistics
import tempfile
import threading
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


# Guards every read-modify-write of the question set (the API serves
# requests on several threads).
_questions_lock = threading.Lock()


def load_questions(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    return json.loads((path or QUESTIONS_FILE).read_text())


def write_json(path: Path, data: Any) -> None:
    """Write ``data`` to ``path`` atomically: a reader (or a kill midway)
    never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def update_questions(change: Any) -> Any:
    """Apply ``change`` (a function of the question list, which it may
    modify) under the lock and save the list; returns what it returned."""
    with _questions_lock:
        path = QUESTIONS_FILE
        questions = load_questions(path) if path.exists() else []
        result = change(questions)
        write_json(path, questions)
        return result


def scorable(questions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Questions ready to score: reviewed ones (``needs_review`` unset) with
    relevant passages, or marked as having no answer."""
    return [
        q
        for q in questions
        if not q.get("needs_review")
        and (q.get("relevant") or q.get("stratum") == "no_answer")
    ]


def question_from_feedback(
    message: Dict[str, Any], existing: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """A rated answer's question as a new evaluation item, flagged for
    review: its cited episodes are only candidates for the relevant ones."""
    cited = [c for c in message.get("citations") or [] if c.get("cited")]
    numbers = [
        int(q["id"][1:])
        for q in existing
        if str(q.get("id", "")).startswith("f") and q["id"][1:].isdigit()
    ]
    return {
        "id": f"f{max(numbers, default=0) + 1:03d}",
        "question": message["question"],
        "stratum": "from_feedback",
        "rating": message["rating"],
        "candidates": sorted({c["episode_id"] for c in cited}),
        "relevant": [],
        "needs_review": True,
        "message_id": message["id"],
    }


def summarize_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """The headline numbers of a retrieval result, for the Admin page."""
    return {
        "overall": result["overall"],
        "strata": {k: v.get("final") for k, v in result["strata"].items()},
        "latency": result["latency"],
        "questions": len(result["questions"]),
    }


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
    understand: Any = None,
) -> Dict[str, Any]:
    """Rank of the first relevant passage for every question at every stage
    (bm25, vector, fused, reranked, final), with recall@k and MRR overall
    and per stratum. No-answer questions are reported by their best
    reranker score instead (for calibrating the abstain threshold).

    With ``understand`` (``Asker.understand``) each question first goes
    through the question step, as in the app: its rewrite, keywords and
    filters are searched. Without it the question is searched as typed."""
    per_question = []
    for item in scorable(questions):
        started = time.monotonic()
        if understand is not None:
            u = understand(item["question"])
            result = retriever.retrieve(
                u.question, username, keywords=u.keywords, filters=u.filters
            )
        else:
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


_JUDGE_SYSTEM = """You grade answers from a question-answering system over \
podcast transcripts. You get the question, possibly a reference answer, the \
system's answer with citations like [3], and the cited passages. Reply with \
JSON only:
{
  "correct": 2 if the answer matches the reference (or, with no reference, \
plainly answers the question), 1 if partly, 0 if not,
  "supported": the share (0 to 1) of the answer's factual sentences that are \
backed by the passage they cite,
  "unsupported": up to 3 short quotes of sentences the cited passages don't back
}
Judge only against the passages and the reference, not your own knowledge."""


def judge_answer(
    judge: LLMClient, item: Dict[str, Any], done: Dict[str, Any]
) -> Dict[str, Any]:
    """The judge's grades for one answer (``done`` from ``Asker.ask``)."""
    cited = [s for s in done["sources"] if s["cited"]]
    passages = "\n\n".join(f"[{s['n']}] {s['text']}" for s in cited) or "(none)"
    user = (
        f"Question: {item['question']}\n"
        + (f"Reference answer: {item['reference']}\n" if item.get("reference") else "")
        + f"\nAnswer:\n{done['answer']}\n\nCited passages:\n{passages}"
    )
    data = _extract_json(judge.chat(_JUDGE_SYSTEM, user, max_tokens=300)) or {}
    correct = _number(data.get("correct"))
    supported = _number(data.get("supported"))
    return {
        "correct": correct if correct in (0, 1, 2) else None,
        "supported": (
            supported if supported is not None and 0 <= supported <= 1 else None
        ),
        "unsupported": [str(u) for u in data.get("unsupported") or []][:3],
        "judge_raw": None if correct in (0, 1, 2) else data,
    }


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate_answers(
    asker: Any,
    judge: LLMClient,
    questions: List[Dict[str, Any]],
    progress_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Ask every question end to end and grade it. No-answer questions pass
    when the answer is declined; the others are graded by the judge for
    correctness and for support by the cited passages. A question that fails
    (or a judge reply that can't be read) is recorded and the run goes on;
    with ``progress_file`` the rows are saved after each question."""
    rows: List[Dict[str, Any]] = []
    for item in scorable(questions):
        try:
            rows.append(_answer_row(asker, judge, item))
        except Exception as e:
            logger.warning("Evaluating %s failed: %s", item.get("id"), e)
            rows.append(
                {"id": item["id"], "stratum": item.get("stratum"), "error": str(e)}
            )
        if progress_file:
            write_json(progress_file, rows)
    return _answer_summary(rows)


def _answer_row(asker: Any, judge: LLMClient, item: Dict[str, Any]) -> Dict[str, Any]:
    done = asker.ask(item["question"])
    row: Dict[str, Any] = {
        "id": item["id"],
        "stratum": item.get("stratum", "lookup"),
        "mode": done.get("mode"),
        "seconds": done.get("timings", {}).get("total"),
        "cited": len(done.get("cited") or []),
        "invalid_citations": len(done.get("invalid_citations") or []),
    }
    if row["stratum"] == "no_answer":
        row["declined"] = done.get("mode") == "not_covered"
    elif done.get("mode") == "answer":
        row.update(judge_answer(judge, item, done))
    return row


def _answer_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    answered = [r for r in rows if r.get("correct") is not None]
    supported = [r["supported"] for r in answered if r.get("supported") is not None]
    no_answer = [
        r for r in rows if r.get("stratum") == "no_answer" and "error" not in r
    ]

    def mean(values: List[float]) -> Optional[float]:
        return statistics.fmean(values) if values else None

    return {
        "answered": len(answered),
        "correct": mean([float(r["correct"]) / 2 for r in answered]),
        "supported": mean([float(v) for v in supported]),
        "uncited": sum(1 for r in answered if not r["cited"]),
        "declined_wrongly": sum(
            1
            for r in rows
            if r.get("stratum") != "no_answer" and r.get("mode") == "not_covered"
        ),
        "no_answer_declined": mean(
            [1.0 if r.get("declined") else 0.0 for r in no_answer]
        ),
        "errors": sum(1 for r in rows if "error" in r),
        "questions": rows,
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
    parser.add_argument(
        "command", choices=["generate", "retrieval", "answers", "compare"]
    )
    parser.add_argument("--db", default="data/p3.duckdb")
    parser.add_argument("--questions", default=str(QUESTIONS_FILE))
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--model", default=None, help="Ollama model for generation")
    parser.add_argument("--no-rerank", action="store_true")
    parser.add_argument(
        "--understand",
        action="store_true",
        help="retrieval: run the question step first, as the app does",
    )
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
        elif args.command == "answers":
            from .ask import Asker
            from .llm import DEFAULT_OLLAMA_MODEL

            llm = LLMClient("ollama", args.model or DEFAULT_OLLAMA_MODEL, num_ctx=8192)
            asker = Asker(db, _retriever(db, not args.no_rerank), llm, None)
            result = evaluate_answers(asker, llm, load_questions(Path(args.questions)))
            out = Path(
                args.out
                or EVAL_DIR / "results" / f"answers-{datetime.now():%Y%m%d-%H%M}.json"
            )
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(result, indent=2, default=str))
            print(
                json.dumps(
                    {k: v for k, v in result.items() if k != "questions"}, indent=2
                )
            )
            print(f"Full results: {out}")
        else:
            retriever = _retriever(db, not args.no_rerank)
            understand = None
            if args.understand:
                from .ask import Asker
                from .llm import DEFAULT_OLLAMA_MODEL

                llm = LLMClient(
                    "ollama", args.model or DEFAULT_OLLAMA_MODEL, num_ctx=8192
                )
                understand = Asker(db, retriever, llm, None).understand
            result = evaluate_retrieval(
                db,
                retriever,
                load_questions(Path(args.questions)),
                understand=understand,
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
