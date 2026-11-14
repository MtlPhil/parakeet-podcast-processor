"""Ask jobs: indexing the library and evaluating retrieval."""

import json
import logging
from datetime import datetime
from typing import Optional

from p3.api.deps import get_db
from p3.api.job_queue import job_runner
from p3.api.tasks.common import _get_settings, _make_embedder
from p3.llm import resolve_provider_and_model
from p3.settings import setting

logger = logging.getLogger(__name__)


def task_index(job_id: str, username: Optional[str] = None):
    """Add every processed episode missing from the Ask index (or embedded
    with another model) to it: the backfill after enabling Ask or changing
    the embedding model. Gives way to a daily run between episodes: it stops
    and queues a new index job for the rest behind it, for the same user."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Indexing the library...")
        from p3.indexer import index_pending

        def on_progress(done: int, total: int, message: str):
            db.update_job(
                job_id, progress=done / total if total else 0, message=message
            )

        stats = index_pending(
            db,
            _make_embedder(_get_settings()),
            progress=on_progress,
            should_stop=lambda: job_runner.priority_waiting,
        )
        left = stats["pending"] - stats["indexed"] - stats["failed"]
        if left > 0:
            next_job = db.create_job("index", username=username)
            job_runner.enqueue(task_index, next_job, username)
        db.update_job(
            job_id,
            status="completed" if not stats["failed"] else "failed",
            progress=1.0,
            message=(
                f"Indexed {stats['indexed']} of {stats['pending']} episodes"
                + (f", {stats['failed']} failed" if stats["failed"] else "")
                + (f"; {left} left for the next index job" if left > 0 else "")
            ),
            error=(
                f"{stats['failed']} episodes could not be indexed (see the log)"
                if stats["failed"]
                else None
            ),
        )
    except Exception as e:
        logger.exception("Index task failed")
        db.update_job(job_id, status="failed", error=str(e))


def task_eval_generate(job_id: str, count: int = 60):
    """Write evaluation questions from the indexed library with the shared
    summarizing LLM: to data/eval/questions.json if there is none yet, else
    to a dated file next to it, so a reviewed set is never overwritten."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Writing questions...")
        from p3.eval_ask import (
            EVAL_DIR,
            QUESTIONS_FILE,
            generate_questions,
            write_json,
        )
        from p3.llm import LLMClient

        settings = _get_settings()
        provider, model = resolve_provider_and_model(settings)
        llm = LLMClient(
            provider=provider,
            model=model,
            ollama_base_url=setting(settings, "ollama_base_url"),
            num_ctx=8192,
        )
        questions = generate_questions(db, llm, count)
        EVAL_DIR.mkdir(parents=True, exist_ok=True)
        out = QUESTIONS_FILE
        try:
            # "x": never replace a question set, even one created meanwhile.
            with open(out, "x") as f:
                json.dump(questions, f, indent=2, ensure_ascii=False)
        except FileExistsError:
            out = EVAL_DIR / f"questions-generated-{datetime.now():%Y%m%d-%H%M%S}.json"
            write_json(out, questions)
        db.update_job(
            job_id,
            status="completed",
            progress=1.0,
            message=f"Wrote {len(questions)} questions to {out} (review them)",
        )
    except Exception as e:
        logger.exception("Question generation failed")
        db.update_job(job_id, status="failed", error=str(e))


def task_eval_retrieval(job_id: str):
    """Score retrieval on data/eval/questions.json; the result goes to
    data/eval/results/."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Evaluating retrieval...")
        from p3.embedder import (
            DEFAULT_RERANKER_MODEL,
            get_reranker,
            release_ask_models,
        )
        from p3.eval_ask import (
            EVAL_DIR,
            evaluate_retrieval,
            load_questions,
            write_json,
        )
        from p3.retriever import Retriever

        settings = _get_settings()
        reranker = (
            get_reranker(settings.get("reranker_model") or DEFAULT_RERANKER_MODEL)
            if settings.get("ask_rerank", True)
            else None
        )
        try:
            result = evaluate_retrieval(
                db, Retriever(db, _make_embedder(settings), reranker), load_questions()
            )
        finally:
            release_ask_models()
        out = EVAL_DIR / "results" / f"retrieval-{datetime.now():%Y%m%d-%H%M%S}.json"
        write_json(out, result)
        final = result["overall"].get("final", {})
        db.update_job(
            job_id,
            status="completed",
            progress=1.0,
            message=(
                f"recall@{result['k']} {final.get('recall', 0):.2f}, "
                f"MRR {final.get('mrr', 0):.2f} on {final.get('n', 0)} questions"
            ),
        )
    except Exception as e:
        logger.exception("Retrieval evaluation failed")
        db.update_job(job_id, status="failed", error=str(e))
