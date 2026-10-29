"""Ask: natural-language questions over the user's library (see p3/ask.py).

``POST /api/ask`` streams Server-Sent Events: ``status`` (stage), ``sources``
(the numbered passages), ``token`` (answer text) and a final ``done`` with
the answer, its citations and the ids of the stored thread and message.
Questions are answered in the request, not on the job queue, so they don't
wait behind a transcription; while one runs, answers fall back to keyword
search, which loads no model.
"""

import json
import logging
import queue
import threading
from threading import Event
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from p3.api.auth import current_user, current_username, require_admin
from p3.api.deps import get_db, user_settings
from p3.api.job_queue import job_runner
from p3.api.models import AskRating, AskRequest
from p3.llm import DEFAULT_OLLAMA_URL, resolve_provider_and_model

router = APIRouter(prefix="/api/ask", tags=["ask"])
ASK_NUM_CTX = 8192
logger = logging.getLogger(__name__)


def _asker(username: str):
    from p3.ask import Asker
    from p3.embedder import (
        DEFAULT_EMBEDDING_MODEL,
        DEFAULT_RERANKER_MODEL,
        Embedder,
        get_reranker,
    )
    from p3.llm import LLMClient
    from p3.retriever import Retriever

    settings = user_settings(username)
    base_url = settings.get("ollama_base_url", DEFAULT_OLLAMA_URL)
    provider, model = resolve_provider_and_model(settings)
    embedder = Embedder(
        settings.get("embedding_model") or DEFAULT_EMBEDDING_MODEL, base_url
    )
    reranker = (
        get_reranker(settings.get("reranker_model") or DEFAULT_RERANKER_MODEL)
        if settings.get("ask_rerank", True)
        else None
    )
    db = get_db()
    return Asker(
        db,
        Retriever(db, embedder, reranker),
        LLMClient(
            provider=provider,
            model=model,
            ollama_base_url=base_url,
            # Ask's prompts (a dozen passages) stay well under this; the
            # default window would reserve several GB more.
            num_ctx=ASK_NUM_CTX,
        ),
        username,
        default_language=settings.get("language") or "en",
    )


def _models_allowed() -> bool:
    from p3.model_gate import models_allowed

    return models_allowed()


_FAILED = {
    "error": "Something went wrong while answering; the error was logged.",
}

# Users with an answer being written, and the thread it's for. Answers run on
# their own thread, so a closed tab or a phone gone to sleep doesn't lose
# them: the reply is stored when it's done and shows when the thread is
# opened again. One at a time per user.
_answering: dict = {}
_answering_lock = threading.Lock()


def answering_thread(username: str):
    """The thread the user's answer in progress is for, or None."""
    with _answering_lock:
        entry = _answering.get(username)
        return entry["thread_id"] if entry else None


def _release(username: str) -> None:
    with _answering_lock:
        _answering.pop(username, None)


@router.post("")
def ask(body: AskRequest, username: str = Depends(current_username)):
    """Answer a question, streamed. Continues ``thread_id`` when given (the
    question step uses its earlier turns), else starts a thread. The first
    event, ``thread``, names the thread, so the page can come back to it."""
    from p3.ask import dumps, history_of

    db = get_db()
    question = body.question.strip()
    if not question:
        raise HTTPException(400, "Ask a question")
    thread = None
    if body.thread_id:
        thread = db.get_ask_thread(body.thread_id, username)
        if not thread:
            raise HTTPException(404, "Conversation not found")
    asker = _asker(username)
    use_models = _models_allowed()
    cancel = Event()
    with _answering_lock:
        if username in _answering:
            raise HTTPException(409, "An answer is still being written; wait for it")
        _answering[username] = {"thread_id": None, "cancel": cancel}
    # The worker's events for this request's stream (None ends it).
    out: "queue.Queue[Optional[dict]]" = queue.Queue()
    try:
        thread_id = thread["id"] if thread else db.create_ask_thread(username, question)
        with _answering_lock:
            _answering[username]["thread_id"] = thread_id
        db.add_ask_message(thread_id, "user", question)
        history = history_of(thread)

        def work():
            # Every question gets a stored reply, so a thread never holds two
            # user turns in a row, and every stream ends with done or error.
            try:
                for event in asker.answer_stream(
                    question,
                    history,
                    use_models,
                    filters={
                        "podcast_ids": body.podcast_ids,
                        "after": body.after,
                        "before": body.before,
                    },
                    cancel=cancel,
                ):
                    if event["event"] == "done":
                        event["thread_id"] = thread_id
                        event["message_id"] = db.add_ask_message(
                            thread_id,
                            "assistant",
                            event["answer"],
                            citations=event["sources"],
                            retrieval={
                                **event["retrieval"],
                                "mode": event["mode"],
                                "understanding": event["understanding"],
                                "scope": event["scope"],
                                "invalid_citations": event["invalid_citations"],
                                "weak_citations": event["weak_citations"],
                            },
                            timings=event["timings"],
                            models=event["models"],
                        )
                        # The stored record is for review, not for the page.
                        event.pop("retrieval", None)
                    out.put(event)
            except Exception:
                logger.exception("Answering failed")
                out.put(
                    {
                        "event": "error",
                        "detail": _FAILED["error"],
                        "thread_id": thread_id,
                    }
                )
                try:
                    db.add_ask_message(
                        thread_id,
                        "assistant",
                        _FAILED["error"],
                        retrieval={"mode": "error"},
                    )
                except Exception:
                    logger.exception("Could not store the failed answer")
            finally:
                _release(username)
                out.put(None)

        threading.Thread(target=work, name="ask", daemon=True).start()
    except BaseException:
        _release(username)
        raise

    def events():
        yield dumps({"event": "thread", "thread_id": thread_id})
        while True:
            event = out.get()
            if event is None:
                return
            yield dumps(event)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/stop")
def stop(username: str = Depends(current_username)):
    """Stop the user's answer in progress: what was written so far is kept.
    (Closing the page doesn't stop it; the answer is stored when done.)"""
    with _answering_lock:
        entry = _answering.get(username)
    if not entry:
        return {"stopped": False}
    entry["cancel"].set()
    return {"stopped": True, "thread_id": entry["thread_id"]}


@router.get("/status")
def ask_status(
    library: bool = Query(
        False, description="Admins: counts for the whole library, not their own"
    ),
    user: dict = Depends(current_user),
):
    """What Ask can do right now, for the page's notices."""
    from p3.embedder import RERANKER_AVAILABLE

    db = get_db()
    everyone = library and user["role"] == "admin"
    return {
        "index": db.index_stats(None if everyone else user["username"]),
        "models_paused": not _models_allowed(),
        "suggestions": db.recent_topics(user["username"]),
        "reranker_available": RERANKER_AVAILABLE,
    }


@router.get("/threads")
def list_threads(username: str = Depends(current_username)):
    return get_db().list_ask_threads(username)


@router.get("/threads/{thread_id}")
def get_thread(thread_id: str, username: str = Depends(current_username)):
    thread = get_db().get_ask_thread(thread_id, username)
    if not thread:
        raise HTTPException(404, "Conversation not found")
    thread["answering"] = answering_thread(username) == thread_id
    for message in thread["messages"]:
        # The retrieval record is for review; the page needs only the mode
        # and the question as the search understood it.
        record = message.pop("retrieval", None) or {}
        message["mode"] = record.get("mode")
        message["standalone"] = (record.get("understanding") or {}).get("question")
        message["scope"] = record.get("scope")
        message["weak_citations"] = record.get("weak_citations") or []
    return thread


@router.delete("/threads/{thread_id}")
def delete_thread(thread_id: str, username: str = Depends(current_username)):
    if answering_thread(username) == thread_id:
        raise HTTPException(409, "Its answer is still being written; stop it first")
    if not get_db().delete_ask_thread(thread_id, username):
        raise HTTPException(404, "Conversation not found")
    return {"deleted": thread_id}


@router.post("/messages/{message_id}/rating")
def rate_message(
    message_id: str, body: AskRating, username: str = Depends(current_username)
):
    if not get_db().rate_ask_message(message_id, username, body.rating):
        raise HTTPException(404, "Answer not found")
    return {"message_id": message_id, "rating": body.rating}


@router.get("/retrieve", dependencies=[Depends(require_admin)])
def retrieve(
    q: str = Query(..., min_length=2),
    username: Optional[str] = Query(
        None, description="Search this user's library (default: everyone's)"
    ),
):
    """Admins: the ranked passages for ``q`` with every stage's scores, no
    answer model. For checking and tuning retrieval."""
    asker = _asker(username or "")
    retrieval = asker.retriever.retrieve(q, username, use_models=_models_allowed())
    return {
        "passages": [
            {
                k: p[k]
                for k in (
                    "id",
                    "episode_id",
                    "episode_title",
                    "podcast_title",
                    "kind",
                    "start_s",
                    "end_s",
                    "text",
                    "score",
                    "rerank_score",
                )
            }
            for p in retrieval.passages
        ],
        **retrieval.summary(),
        "timings": retrieval.timings,
    }


@router.post("/index", dependencies=[Depends(require_admin)])
def index_library():
    """Admins: queue indexing of every processed episode not yet in the Ask
    index (or embedded with another model)."""
    from p3.api.tasks import task_index

    job_id = get_db().create_job("index")
    job_runner.enqueue(task_index, job_id)
    return {"job_id": job_id}


# ----------------------------------------------------------------------
# Quality (admins): evaluation runs and rated answers
# ----------------------------------------------------------------------


@router.get("/eval", dependencies=[Depends(require_admin)])
def eval_overview():
    """The question set's size by stratum and the latest retrieval results."""
    from p3.eval_ask import EVAL_DIR, QUESTIONS_FILE, scorable, summarize_result

    questions = (
        json.loads(QUESTIONS_FILE.read_text()) if QUESTIONS_FILE.exists() else []
    )
    strata: dict = {}
    for q in questions:
        strata[q.get("stratum", "lookup")] = (
            strata.get(q.get("stratum", "lookup"), 0) + 1
        )
    results = []
    for path in sorted((EVAL_DIR / "results").glob("retrieval-*.json"), reverse=True)[
        :5
    ]:
        try:
            results.append(
                {"file": path.name, **summarize_result(json.loads(path.read_text()))}
            )
        except (ValueError, KeyError):
            continue
    return {
        "questions": len(questions),
        "scorable": len(scorable(questions)),
        "needs_review": sum(1 for q in questions if q.get("needs_review")),
        "strata": strata,
        "results": results,
    }


@router.post("/eval/generate", dependencies=[Depends(require_admin)])
def eval_generate(count: int = Query(60, ge=5, le=300)):
    from p3.api.tasks import task_eval_generate

    job_id = get_db().create_job("eval_generate")
    job_runner.enqueue(task_eval_generate, job_id, count)
    return {"job_id": job_id}


@router.post("/eval/run", dependencies=[Depends(require_admin)])
def eval_run():
    from p3.api.tasks import task_eval_retrieval
    from p3.eval_ask import QUESTIONS_FILE

    if not QUESTIONS_FILE.exists():
        raise HTTPException(400, "No question set yet: generate one first")
    job_id = get_db().create_job("eval_retrieval")
    job_runner.enqueue(task_eval_retrieval, job_id)
    return {"job_id": job_id}


@router.get("/feedback", dependencies=[Depends(require_admin)])
def feedback(limit: int = Query(50, ge=1, le=200)):
    """Rated answers, newest first."""
    return get_db().rated_ask_messages(limit)


@router.post("/feedback/{message_id}/to-eval", dependencies=[Depends(require_admin)])
def feedback_to_eval(message_id: str):
    """Add a rated answer's question to the evaluation set, flagged for
    review (its relevant passages still have to be marked)."""
    from p3 import eval_ask

    message = get_db().rated_ask_message(message_id)
    if not message:
        raise HTTPException(404, "Rated answer not found")

    def add(questions: list):
        if any(q.get("message_id") == message_id for q in questions):
            return None
        item = eval_ask.question_from_feedback(message, questions)
        questions.append(item)
        return item

    item = eval_ask.update_questions(add)
    if item is None:
        raise HTTPException(409, "Already in the evaluation set")
    return item
