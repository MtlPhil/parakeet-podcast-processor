"""Red-team probes for Ask, second pass (code added since d563eb6). Each test
states the behaviour a professional product should have; a failing test is
a demonstrated defect.

No model, network or real database is touched: fakes and temp DBs only.
"""

import os
import threading
import time
from datetime import datetime

import pytest

from p3 import model_gate
from p3.ask import Asker, fit_passages
from p3.chunker import notes_text
from p3.database import P3Database
from p3.indexer import index_episode
from p3.retriever import Retriever
from tests.test_ask import (
    FakeEmbedder,
    FakeLLM,
    FakeReranker,
    _client,
    _library,
    _segments,
)


@pytest.fixture
def db(tmp_path):
    database = P3Database(str(tmp_path / "rt2.duckdb"))
    yield database
    database.close()


def _episode(db, title, texts, description=None, url=None):
    p = db.add_podcast(f"{title} show", f"http://example.com/{title}")
    e = db.add_episode(
        p,
        title,
        datetime(2026, 10, 1),
        url or f"http://x/{title}",
        description=description,
    )
    db.add_transcript_segments(e, _segments(texts))
    db.update_episode_status(e, "processed")
    index_episode(db, e, FakeEmbedder())
    return e


# ----------------------------------------------------------------------
# 1. The model gate is held while the client reads the stream
# ----------------------------------------------------------------------


class TestGate:
    def test_a_slow_reader_does_not_hold_transcription_back(self, db):
        """`with model_use():` wraps the `yield` of every answer token, so the
        gate stays held while the generator waits for the client to take
        the next event (a phone gone to sleep, a stalled proxy). A
        transcription then waits _DRAIN_SECONDS (180 s) and goes ahead with
        the call still counted, or fails at the Ollama release. The same
        holds for Gemini answers, which use no local memory at all."""
        _library(db)
        asker = Asker(
            db,
            Retriever(db, FakeEmbedder(), FakeReranker()),
            FakeLLM(answer="Rates went up [1]. " * 20),
            None,
        )
        stream = asker.answer_stream("What did the bank do on rates?")
        try:
            for event in stream:
                if event["event"] == "token":
                    break  # the client hasn't read further yet
            drained = model_gate.pause_for_transcription(timeout=0.3)
        finally:
            model_gate.resume()
            stream.close()
        assert drained, "an unread answer stream blocks transcription"


# ----------------------------------------------------------------------
# 2. Deleting an episode while it is being (re)indexed
# ----------------------------------------------------------------------


class TestIndexTransaction:
    def test_purge_during_reindex_completes(self, db, monkeypatch):
        """replace_chunks now deletes and inserts in one transaction, but
        purge_episode doesn't take _chunk_lock. A user deleting an episode
        while the digest job or the index backfill rewrites its passages
        gets "Conflict on tuple deletion!" halfway through the purge: the
        transcript and summary are gone, the episode row and its passages
        stay (with no owner, so nothing ever purges them again)."""
        from p3 import chunker

        e1, e2 = _library(db)
        entered, release = threading.Event(), threading.Event()
        real = chunker.term_counts

        def slow_counts(text):
            entered.set()
            release.wait(5)
            return real(text)

        monkeypatch.setattr(chunker, "term_counts", slow_counts)
        writer = threading.Thread(target=index_episode, args=(db, e2, FakeEmbedder()))
        writer.start()
        assert entered.wait(5)
        error = None
        try:
            db.purge_episode(e2)
        except Exception as e:  # noqa: BLE001 - the probe reports it
            error = f"{type(e).__name__}: {e}"
        finally:
            release.set()
            writer.join(10)
        assert error is None, error
        assert db.get_episode_by_id(e2) is None


# ----------------------------------------------------------------------
# 3. Show notes that index to nothing are stale forever
# ----------------------------------------------------------------------


class TestShowNotes:
    def test_blank_html_notes_dont_keep_an_episode_pending(self, db):
        """episodes_to_index counts description <> '' as "has notes", but
        notes_text("<p>&nbsp;</p>") is "", so no notes passage is ever
        written. The episode is re-embedded by every index job, and
        _queue_index_catch_up queues one after every daily run, forever."""
        e = _episode(db, "blank", ["hello rates"], description="<p>&nbsp;</p>")
        assert e not in db.episodes_to_index("fake-embed")

    def test_whitespace_notes_dont_keep_an_episode_pending(self, db):
        e = _episode(db, "spaces", ["hello rates"], description="  \n ")
        assert e not in db.episodes_to_index("fake-embed")

    def test_entity_encoded_html_is_stripped(self):
        """Many feeds escape their HTML once more: tags are removed before
        entities are decoded, so the markup ends up in the passage."""
        assert notes_text(
            "&lt;p&gt;Guest: &lt;a href='https://x.co'&gt;Ann&lt;/a&gt;"
        ) == ("Guest: Ann")


# ----------------------------------------------------------------------
# 4. Duplicate-passage fingerprint drops distinct passages
# ----------------------------------------------------------------------


SPONSOR = (
    "This episode is brought to you by Squarespace, build beautiful websites "
    "online stores portfolios blogs; visit squarespace dot com slash podcast "
    "for ten percent off your first purchase of a website or domain today. "
) * 3


class TestDuplicates:
    def test_passages_sharing_an_opening_ad_are_both_kept(self, db):
        """The fingerprint is the first 400 characters of the passage's
        tokens. Two different episodes whose passage opens with the same ad
        read (or the same show-notes boilerplate) collide, and the second
        one's actual content is never retrieved."""
        a = _episode(db, "a", [SPONSOR, "The central bank raised rates."])
        b = _episode(db, "b", [SPONSOR, "Inflation fell to two percent."])
        retrieval = Retriever(db).retrieve("rates inflation", None)
        assert {p["episode_id"] for p in retrieval.passages} >= {a, b}


# ----------------------------------------------------------------------
# 5. Prompt size: the character budget isn't a token budget
# ----------------------------------------------------------------------


class TestFit:
    def test_cjk_passages_fit_the_8k_window(self):
        """PASSAGES_BUDGET_CHARS assumes ~4 characters a token. CJK text
        (now indexed, see the tokenizer) runs at 1-2 characters a token, so
        22,000 characters is 11k-22k tokens in an 8,192-token window (and
        Ollama drops the start of the prompt, where the rules are)."""
        passages = [{"text": "日本銀行は金利を引き上げた。" * 120} for _ in range(30)]
        kept = fit_passages(passages)
        chars = sum(len(p["text"]) for p in kept)
        # Generous: 2 CJK characters a token, 1,200 for the answer.
        assert chars / 2 <= 8192 - 1200


# ----------------------------------------------------------------------
# 6. Podcast filter: subset matching still mixes similar titles
# ----------------------------------------------------------------------


def test_planet_money_does_not_also_select_money(db):
    money = db.add_podcast("Money", "http://example.com/money")
    planet = db.add_podcast("Planet Money", "http://example.com/planet")
    asker = Asker(db, Retriever(db), None, None)
    assert asker._filters({"podcasts": ["Planet Money"]}) == {
        "podcast_ids": [planet]
    }, f"Money ({money}) is selected too"


# ----------------------------------------------------------------------
# 7. Evaluation set and judge
# ----------------------------------------------------------------------


@pytest.fixture
def quality(tmp_path, monkeypatch):
    client, deps = _client(tmp_path, monkeypatch)
    from p3 import eval_ask

    monkeypatch.setattr(eval_ask, "EVAL_DIR", tmp_path / "eval")
    monkeypatch.setattr(eval_ask, "QUESTIONS_FILE", tmp_path / "eval" / "q.json")
    db = deps.get_db()
    client.get("/api/me")
    yield client, db, eval_ask
    deps.close_db()
    if os.path.exists(tmp_path / "api.duckdb"):
        os.remove(tmp_path / "api.duckdb")


def _rated(db, question):
    thread = db.create_ask_thread("tester", question)
    db.add_ask_message(thread, "user", question)
    message = db.add_ask_message(thread, "assistant", "An answer", citations=[])
    db.rate_ask_message(message, "tester", -1)
    return message


class TestEvalSet:
    def test_concurrent_to_eval_requests_keep_both(self, quality, monkeypatch):
        """feedback_to_eval reads questions.json, appends, writes it back,
        with no lock: two admins (or two quick clicks, FastAPI runs sync
        endpoints in a thread pool) lose one item, or, as this test shows,
        interleave their write_text calls and leave questions.json as
        invalid JSON ("Extra data"), after which /eval, /eval/run and
        to-eval all fail. task_eval_generate's exists-then-write races the
        same way, and a kill mid-write corrupts the hand-reviewed file."""
        _, db, eval_ask = quality
        from p3.api.routers import ask as ask_router

        m1, m2 = _rated(db, "first?"), _rated(db, "second?")
        real = eval_ask.question_from_feedback

        def slow(message, existing):
            # Without a lock both requests read the file during this pause
            # (the original probe used a barrier, which a correct lock makes
            # unreachable); with one, they take turns.
            time.sleep(0.2)
            return real(message, existing)

        monkeypatch.setattr(eval_ask, "question_from_feedback", slow)
        threads = [
            threading.Thread(target=ask_router.feedback_to_eval, args=(m,))
            for m in (m1, m2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        stored = eval_ask.load_questions(eval_ask.QUESTIONS_FILE)
        assert {q["message_id"] for q in stored} == {m1, m2}

    def test_feedback_ids_are_unique(self):
        """Ids come from len(existing): after a reviewer deletes an item,
        the next one reuses an id, and comparisons keyed by id (reciprocal
        ranks, paired bootstrap) silently merge two questions."""
        from p3.eval_ask import question_from_feedback

        existing = [{"id": "f002", "question": "kept"}]
        item = question_from_feedback(
            {"id": "m", "question": "q", "rating": -1, "citations": []}, existing
        )
        assert item["id"] not in {q["id"] for q in existing}

    def test_one_odd_judge_reply_doesnt_lose_the_run(self, db):
        """evaluate_answers does float(r["correct"]) on whatever the judge
        returned; one "partly" (or a judge timeout, which isn't caught
        either) raises after every question was answered, and nothing is
        written: the results file is only saved at the end."""
        from p3.eval_ask import evaluate_answers

        e1, _ = _library(db)

        class Judge:
            provider, model, is_configured = "fake", "judge", True

            def chat(self, system, user, max_tokens=300):
                return '{"correct": "partly", "supported": "most"}'

        asker = Asker(
            db, Retriever(db, FakeEmbedder(), FakeReranker()), FakeLLM(), None
        )
        questions = [
            {
                "id": "q1",
                "question": "bank rates",
                "stratum": "lookup",
                "relevant": [{"episode_id": e1}],
            }
        ]
        result = evaluate_answers(asker, Judge(), questions)
        assert result["questions"][0]["id"] == "q1"
