"""Ask: natural-language questions over the user's library (see p3/ask.py).

``POST /api/ask`` streams Server-Sent Events: ``status`` (stage), ``sources``
(the numbered passages), ``token`` (answer text) and a final ``done`` with
the answer, its citations and the ids of the stored thread and message.
Questions are answered in the request, not on the job queue, so they don't
wait behind a transcription; while one runs, answers fall back to keyword
search, which loads no model.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from p3.api.auth import current_username, require_admin
from p3.api.deps import get_db, user_settings
from p3.api.job_queue import job_runner
from p3.api.models import AskRating, AskRequest
from p3.llm import DEFAULT_OLLAMA_URL, resolve_provider_and_model

router = APIRouter(prefix="/api/ask", tags=["ask"])


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
        LLMClient(provider=provider, model=model, ollama_base_url=base_url),
        username,
    )


def _models_allowed() -> bool:
    from p3.api.tasks import transcribing

    return not transcribing.is_set()


@router.post("")
def ask(body: AskRequest, username: str = Depends(current_username)):
    """Answer a question, streamed. Continues ``thread_id`` when given (the
    question step uses its earlier turns), else starts a thread."""
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

    def events():
        thread_id = thread["id"] if thread else db.create_ask_thread(username, question)
        history = history_of(thread)
        db.add_ask_message(thread_id, "user", question)
        try:
            for event in asker.answer_stream(question, history, use_models):
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
                            "invalid_citations": event["invalid_citations"],
                        },
                        timings=event["timings"],
                        models=event["models"],
                    )
                    # The stored record is for review, not for the page.
                    event.pop("retrieval", None)
                yield dumps(event)
        except Exception as e:  # the stream has started: report in-band
            yield dumps({"event": "error", "detail": str(e), "thread_id": thread_id})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/status")
def ask_status(username: str = Depends(current_username)):
    """What Ask can do right now, for the page's notices."""
    from p3.embedder import RERANKER_AVAILABLE

    db = get_db()
    return {
        "index": db.index_stats(),
        "models_paused": not _models_allowed(),
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
    for message in thread["messages"]:
        # The retrieval record is for review; the page only needs the mode.
        message["mode"] = (message.pop("retrieval", None) or {}).get("mode")
    return thread


@router.delete("/threads/{thread_id}")
def delete_thread(thread_id: str, username: str = Depends(current_username)):
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
