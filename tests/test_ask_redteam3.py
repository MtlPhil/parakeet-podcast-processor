"""Red-team probes for Ask, third pass (code added since 7d9d7e1). Each test
states the behaviour a professional product should have; a failing test is
a demonstrated defect.

No model, network or real database is touched: fakes and temp DBs only.
"""

import os
import types

import pytest

from p3.ask import Asker, format_passages
from p3.chunker import build_chunks
from p3.database import P3Database
from p3.retriever import Retriever
from tests.test_ask import (
    FakeEmbedder,
    FakeLLM,
    FakeReranker,
    _client,
    _events,
    _library,
)


@pytest.fixture
def db(tmp_path):
    database = P3Database(str(tmp_path / "rt3.duckdb"))
    yield database
    database.close()


@pytest.fixture
def api(tmp_path, monkeypatch):
    client, deps = _client(tmp_path, monkeypatch)
    db = deps.get_db()
    client.get("/api/me")  # tester: first user, admin
    _library(db, username="tester")
    from p3.api.routers import ask as ask_router

    monkeypatch.setattr(
        ask_router,
        "_asker",
        lambda username: Asker(
            db, Retriever(db, FakeEmbedder(), FakeReranker()), FakeLLM(), username
        ),
    )
    yield client, db, ask_router
    with ask_router._answering_lock:
        ask_router._answering.clear()
    deps.close_db()
    if os.path.exists(tmp_path / "api.duckdb"):
        os.remove(tmp_path / "api.duckdb")


# ----------------------------------------------------------------------
# 1. Background answers: failure paths
# ----------------------------------------------------------------------


class TestAnswerWorker:
    def test_stream_always_ends_with_done_or_error(self, api, monkeypatch):
        """When storing the reply fails (the DB is often what failed in the
        first place), the worker's except block raises on its own
        add_ask_message, the error event is never queued, and the stream
        just ends. The page keeps the answer as `streaming` and shows
        Pending with its seconds counter forever."""
        client, db, _ = api
        real = db.add_ask_message

        def failing(thread_id, role, *a, **k):
            if role == "assistant":
                raise RuntimeError("database is gone")
            return real(thread_id, role, *a, **k)

        monkeypatch.setattr(db, "add_ask_message", failing)
        events = _events(client.post("/api/ask", json={"question": "rates?"}).text)
        assert events[-1]["event"] in ("done", "error"), [e["event"] for e in events]

    def test_registry_released_when_the_worker_cannot_start(self, api, monkeypatch):
        """_answering is filled, the user turn stored, then Thread.start()
        runs outside any try: if it raises (can't start new thread), the
        user is refused with 409 until the server restarts."""
        client, _, ask_router = api

        def no_thread(*a, **k):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(
            ask_router, "threading", types.SimpleNamespace(Thread=no_thread)
        )
        try:
            client.post("/api/ask", json={"question": "rates?"})
        except RuntimeError:
            pass
        assert ask_router.answering_thread("tester") is None


# ----------------------------------------------------------------------
# 2. Purge racing the indexer: passages for an erased episode
# ----------------------------------------------------------------------


def test_no_passages_survive_a_purge_during_indexing(db, monkeypatch):
    """purge_episode now waits on _chunk_lock, but index_episode reads the
    episode, transcript and summary before taking it. A purge landing in
    between finishes first; replace_chunks then writes passages (no
    foreign key) for an episode that no longer exists, kept forever."""
    from p3 import indexer

    _, e2 = _library(db)
    real = indexer.build_chunks

    def purge_meanwhile(*a, **k):
        db.purge_episode(e2)  # the user deletes it while it's indexed
        return real(*a, **k)

    monkeypatch.setattr(indexer, "build_chunks", purge_meanwhile)
    indexer.index_episode(db, e2, FakeEmbedder())
    left = db.conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE episode_id = ?", (e2,)
    ).fetchone()[0]
    assert left == 0


# ----------------------------------------------------------------------
# 3. Quotes placed in the transcript
# ----------------------------------------------------------------------


def _ep(**extra):
    return {
        "title": "Ep",
        "podcast_title": "Show",
        "date": None,
        "language": "en",
        **extra,
    }


def _segs(texts):
    return [
        {"timestamp_start": i * 20.0, "timestamp_end": (i + 1) * 20.0, "text": t}
        for i, t in enumerate(texts)
    ]


class TestQuotes:
    def test_timed_quote_is_still_labelled_a_quote_for_the_model(self):
        """A located quote gets start_s/end_s, and format_passages labels
        any passage with a start time as a transcript moment, dropping the
        "quote picked from the episode" label. The digest's (often
        reworded) quote is then presented as words said at that time."""
        quote = {
            "kind": "quote",
            "start_s": 60.0,
            "end_s": 120.0,
            "text": "Rates will stay high for longer than people think.",
            "podcast_title": "Show",
            "episode_title": "Ep",
            "episode_date": None,
        }
        assert "quote" in format_passages([quote]).split("\n", 1)[0]

    def test_quote_repeated_in_show_notes_doesnt_break_indexing(self):
        """`transcript = list(chunks)` is taken after the show notes are
        appended, so locate_quote can pick the notes passage when the notes
        repeat a quote (common: notes quote the guest). That passage has no
        "start_s" key yet (setdefault runs at the end), so build_chunks
        raises KeyError: index_episode fails, _index_episode's keyword-only
        retry calls build_chunks again and fails too, and the episode is
        never indexed ("not indexed: 'start_s'"), every index job."""
        said = "the central bank will keep rates high through next spring"
        reworded = "central bank keeps rates high through next spring honestly"
        try:
            chunks = build_chunks(
                _ep(description=f'Our guest says "{said}".'),
                _segs([reworded]),
                {"quotes": [said]},
            )
        except KeyError as e:
            pytest.fail(f"build_chunks raised KeyError {e}")
        [quote] = [c for c in chunks if c["kind"] == "quote"]
        assert quote["start_s"] is not None  # it is in the transcript too


# ----------------------------------------------------------------------
# 4. The junk-topics filter only catches stopwords
# ----------------------------------------------------------------------


def test_word_frequency_topics_are_not_indexed():
    """_basic_extraction's topics are the five most frequent words longer
    than four letters, plus the theme "general discussion". Only words in
    the stopword list are filtered, so a typical fallback ("people;
    actually; something; there; because") still becomes a topics passage
    that matches any question with "people" in it."""
    summary = {
        "key_topics": ["people", "actually", "something", "there", "because"],
        "themes": ["general discussion"],
    }
    chunks = build_chunks(_ep(), _segs(["hello"]), summary)
    assert not [c for c in chunks if c["kind"] == "topics"]
