"""Tests for Ask: chunking, the BM25 and vector index, fusion, citations,
the answer flow (with fake models) and the API.

No model is loaded: the embedder, reranker and LLM are stand-ins.
"""

import json
import os
import re
from datetime import datetime

import pytest

from p3.ask import (
    ANSWER_PROMPT_VERSION,
    UNDERSTAND_PROMPT_VERSION,
    Asker,
    validate_citations,
)
from p3.chunker import build_chunks, tokenize, transcript_chunks
from p3.database import P3Database
from p3.indexer import index_episode, index_pending
from p3.retriever import Retriever, limit_per_episode, reciprocal_rank_fusion


@pytest.fixture
def db(tmp_path):
    database = P3Database(str(tmp_path / "test.duckdb"))
    yield database
    database.close()


def _segments(texts, length=20.0):
    """Segments in both shapes: stored (start/end) and read back
    (timestamp_start/timestamp_end)."""
    return [
        {
            "start": i * length,
            "end": (i + 1) * length,
            "timestamp_start": i * length,
            "timestamp_end": (i + 1) * length,
            "text": t,
        }
        for i, t in enumerate(texts)
    ]


class FakeEmbedder:
    """Bag-of-words vectors over a tiny vocabulary: similar enough for tests."""

    model = "fake-embed"
    VOCAB = ["bank", "rates", "inflation", "apple", "phone", "music", "taux"]

    def embed(self, texts):
        vectors = []
        for text in texts:
            words = tokenize(text)
            v = [float(words.count(w)) for w in self.VOCAB] + [0.01]
            norm = sum(x * x for x in v) ** 0.5
            vectors.append([x / norm for x in v])
        return vectors


class FakeReranker:
    model_name = "fake-rerank"

    def __init__(self, scores=None):
        self.scores = scores  # fixed score for every passage, or None

    def score(self, question, passages):
        if self.scores is not None:
            return [self.scores] * len(passages)
        q = set(tokenize(question))
        return [len(q & set(tokenize(p))) / (len(q) or 1) for p in passages]


class FakeLLM:
    provider = "fake"
    model = "fake-llm"
    is_configured = True

    def __init__(self, answer="Rates went up [1]. Also [9].", understanding=None):
        self.answer = answer
        self.understanding = understanding
        self.prompts = []

    def chat(self, system, user, max_tokens=2000):
        self.prompts.append(user)
        return json.dumps(
            self.understanding
            or {"question": None, "language": None, "keywords": [], "podcasts": []}
        )

    def chat_stream(self, system, user, max_tokens=2000):
        self.prompts.append(user)
        yield from re.findall(r"\S+\s*", self.answer)


def _library(db, username=None):
    """Two podcasts, one episode each, indexed with the fake embedder."""
    p1 = db.add_podcast("Money Talk", "http://example.com/money")
    p2 = db.add_podcast("Tech Weekly", "http://example.com/tech")
    e1 = db.add_episode(p1, "The Bank of Canada", datetime(2026, 9, 1), "http://x/1")
    e2 = db.add_episode(p2, "New phones", datetime(2026, 10, 1), "http://x/2")
    db.add_transcript_segments(
        e1,
        _segments(
            [
                "The Bank of Canada raised interest rates again.",
                "Inflation is still above target, said the governor.",
                "Mortgage holders will feel the rates.",
            ]
        ),
    )
    db.add_transcript_segments(
        e2,
        _segments(["Apple released a new phone.", "The phone camera is better."]),
    )
    for e in (e1, e2):
        db.update_episode_status(e, "processed")
        if username:
            db.ensure_user(username)
            db.link_episode(username, e)
        index_episode(db, e, FakeEmbedder())
    return e1, e2


# ----------------------------------------------------------------------
# Chunking and tokens
# ----------------------------------------------------------------------


class TestChunking:
    def test_tokenize_folds_accents_and_drops_stopwords(self):
        assert tokenize("Les taux d'intérêt de la Banque") == [
            "taux",
            "interet",
            "banque",
        ]
        assert tokenize("The 2 rates") == ["2", "rates"]

    def test_passages_cover_about_a_minute_and_overlap_by_one_segment(self):
        chunks = transcript_chunks(_segments([f"s{i}" for i in range(7)]))
        assert [(c["start_s"], c["end_s"]) for c in chunks] == [
            (0, 60),
            (40, 100),
            (80, 140),
        ]
        assert chunks[1]["text"].startswith("s2")

    def test_long_segment_never_stalls(self):
        chunks = transcript_chunks(_segments(["a", "b", "c"], length=120.0))
        assert [c["text"] for c in chunks] == ["a", "b", "c"]

    def test_summary_topics_and_quotes_become_passages(self):
        episode = {"title": "E", "podcast_title": "P", "date": None, "language": "fr"}
        summary = {
            "full_summary": "Short summary.",
            "key_topics": '["rates", "housing"]',
            "themes": ["economy"],
            "quotes": '["We will hike."]',
        }
        chunks = build_chunks(episode, _segments(["hello"]), summary)
        assert [c["kind"] for c in chunks] == [
            "transcript",
            "summary",
            "topics",
            "quote",
        ]
        assert chunks[2]["text"] == "rates; housing; economy"
        assert [c["seq"] for c in chunks] == [0, 1, 2, 3]
        assert all(c["prefix"] == "P: E" and c["language"] == "fr" for c in chunks)


# ----------------------------------------------------------------------
# Index and search
# ----------------------------------------------------------------------


class TestIndex:
    def test_bm25_finds_the_matching_passage_first(self, db):
        e1, e2 = _library(db)
        [(best, score), *_] = db.bm25_search(tokenize("interest rates"), None)
        assert db.get_chunks([best])[best]["episode_id"] == e1
        assert score > 0
        assert db.bm25_search(tokenize("unrelated words"), None) == []

    def test_search_is_limited_to_the_users_library(self, db):
        e1, e2 = _library(db, username="owner")  # the first user takes over all
        db.ensure_user("ann")
        db.link_episode("ann", e2)
        assert db.bm25_search(tokenize("rates"), "ann") == []
        hits = db.vector_search(FakeEmbedder().embed(["rates"])[0], "fake-embed", "ann")
        assert {db.get_chunks([i])[i]["episode_id"] for i, _ in hits} == {e2}

    def test_filters(self, db):
        e1, e2 = _library(db)
        terms = tokenize("rates phone")
        only_tech = db.bm25_search(terms, None, {"podcast_ids": [2]})
        assert {db.get_chunks([i])[i]["episode_id"] for i, _ in only_tech} == {e2}
        after = db.bm25_search(terms, None, {"after": "2026-09-15"})
        assert {db.get_chunks([i])[i]["episode_id"] for i, _ in after} == {e2}
        before = db.bm25_search(terms, None, {"before": "2026-09-01"})
        assert {db.get_chunks([i])[i]["episode_id"] for i, _ in before} == {e1}

    def test_reindex_replaces_and_purge_deletes(self, db):
        e1, _ = _library(db)
        count = db.index_stats()["chunks"]
        index_episode(db, e1, FakeEmbedder())
        assert db.index_stats()["chunks"] == count
        db.purge_episode(e1)
        assert all(
            c["episode_id"] != e1 for c in db.get_chunks(list(range(1, 50))).values()
        )
        assert (
            db.conn.execute(
                "SELECT COUNT(*) FROM chunk_terms WHERE episode_id = ?", (e1,)
            ).fetchone()[0]
            == 0
        )

    def test_pending_tracks_the_embedding_model(self, db):
        e1, e2 = _library(db)
        assert db.episodes_to_index("fake-embed") == []
        assert db.episodes_to_index("other-model") == [e1, e2]
        index_episode(db, e1, None)  # keyword-only
        assert db.episodes_to_index("fake-embed") == [e1]

    def test_index_pending_stops_when_asked(self, db):
        _library(db)
        stats = index_pending(db, FakeEmbedder(), should_stop=lambda: True)
        assert stats == {"indexed": 0, "failed": 0, "pending": 0}


class TestFusion:
    def test_rrf_rewards_agreement(self):
        fused = reciprocal_rank_fusion([[1, 2, 3], [3, 1]])
        assert [i for i, _ in fused] == [1, 3, 2]

    def test_rrf_ties_keep_first_appearance(self):
        assert [i for i, _ in reciprocal_rank_fusion([[5], [6]])] == [5, 6]

    def test_limit_per_episode(self):
        ranked = [(1, 0.9), (2, 0.8), (3, 0.7), (4, 0.6)]
        episodes = {1: 10, 2: 10, 3: 10, 4: 20}
        assert limit_per_episode(ranked, episodes, top_k=3, per_episode=2) == [
            (1, 0.9),
            (2, 0.8),
            (4, 0.6),
        ]

    def test_retriever_degrades_without_models(self, db):
        _library(db)

        class Broken:
            model = "fake-embed"

            def embed(self, texts):
                raise RuntimeError("ollama down")

        result = Retriever(db, Broken(), None).retrieve("bank rates", None)
        assert result.passages
        assert result.skipped == {"vector": "ollama down", "rerank": "no reranker"}

    def test_repeated_passages_are_kept_once(self, db):
        e1, _ = _library(db)
        p = db.add_podcast("Money Talk again", "http://example.com/money2")
        copy = db.add_episode(
            p, "The Bank of Canada", datetime(2026, 9, 2), "http://x/1b"
        )
        db.add_transcript_segments(
            copy,
            db.get_transcripts_for_episode(e1)
            and _segments(
                [
                    "The Bank of Canada raised interest rates again.",
                    "Inflation is still above target, said the governor.",
                    "Mortgage holders will feel the rates.",
                ]
            ),
        )
        db.update_episode_status(copy, "processed")
        index_episode(db, copy, FakeEmbedder())
        result = Retriever(db, FakeEmbedder(), None).retrieve(
            "bank interest rates", None
        )
        texts = [p["text"] for p in result.passages]
        assert len(texts) == len(set(texts))

    def test_retriever_reranks(self, db):
        e1, _ = _library(db)
        result = Retriever(db, FakeEmbedder(), FakeReranker()).retrieve(
            "bank rates inflation", None
        )
        assert result.passages[0]["episode_id"] == e1
        assert result.passages[0]["rerank_score"] > 0
        assert "reranked" in result.stages


# ----------------------------------------------------------------------
# Answers
# ----------------------------------------------------------------------


class TestCitations:
    def test_invalid_numbers_are_removed(self):
        checked = validate_citations("A [1]. B [2, 7]. C [9].", passage_count=3)
        assert checked["answer"] == "A [1]. B [2]. C ."
        assert checked["cited"] == [1, 2]
        assert checked["invalid"] == [7, 9]


class TestAsker:
    def _asker(self, db, llm=None, reranker=None, threshold=0.05):
        return Asker(
            db,
            Retriever(db, FakeEmbedder(), reranker or FakeReranker()),
            llm or FakeLLM(),
            None,
            abstain_threshold=threshold,
        )

    def test_answers_with_validated_citations(self, db):
        _library(db)
        llm = FakeLLM()
        events = list(
            self._asker(db, llm).answer_stream("What did the bank do on rates?")
        )
        assert [e["event"] for e in events][:3] == ["status", "status", "sources"]
        done = events[-1]
        assert done["mode"] == "answer"
        assert done["answer"] == "Rates went up [1]. Also ."
        assert done["cited"] == [1] and done["invalid_citations"] == [9]
        assert done["sources"][0]["cited"] is True
        assert "<<<" in llm.prompts[-1]  # passages are fenced as quoted data
        assert done["models"]["prompts"] == [
            UNDERSTAND_PROMPT_VERSION,
            ANSWER_PROMPT_VERSION,
        ]

    def test_not_covered_skips_the_answer_model(self, db):
        _library(db)
        llm = FakeLLM()
        done = self._asker(db, llm, FakeReranker(scores=0.001)).ask("Bank rates?")
        assert done["mode"] == "not_covered"
        assert done["answer"].startswith("Your library doesn't seem to cover this.")
        assert len(llm.prompts) == 1  # only the question step

    def test_answer_follows_the_question_language(self, db):
        _library(db)
        llm = FakeLLM(
            understanding={
                "question": "Que dit la banque sur les taux?",
                "language": "fr",
            }
        )
        done = self._asker(db, llm, FakeReranker(scores=0.001)).ask(
            "Que dit la banque sur les taux?"
        )
        assert done["language"] == "fr"
        assert done["answer"].startswith("Votre bibliothèque")

    def test_paused_mode_uses_keyword_search_only(self, db):
        _library(db)
        llm = FakeLLM()
        done = self._asker(db, llm).ask("bank rates", use_models=False)
        assert done["mode"] == "paused"
        assert done["sources"]
        assert llm.prompts == []

    def test_filters_from_the_question_step(self, db):
        e1, e2 = _library(db)
        llm = FakeLLM(
            understanding={
                "question": "phone news",
                "language": "en",
                "keywords": ["phone"],
                "podcasts": ["tech weekly", "a podcast nobody has"],
                "after": "2026-09-15",
                "before": "not a date",
            }
        )
        done = self._asker(db, llm).ask("phone news on tech weekly lately")
        assert done["understanding"]["filters"] == {
            "podcast_ids": [2],
            "after": "2026-09-15",
        }
        assert {s["episode_id"] for s in done["sources"]} == {e2}

    def test_broad_questions_group_passages_by_episode(self, db):
        e1, e2 = _library(db)
        db.add_summary(e1, ["rates"], ["economy"], [], [], "The bank raised rates.")
        index_episode(db, e1, FakeEmbedder())
        llm = FakeLLM(
            understanding={
                "question": "Overview of rates talk",
                "language": "en",
                "scope": "broad",
                "keywords": ["rates", "bank"],
            }
        )
        done = self._asker(db, llm).ask("What has been said about rates overall?")
        assert done["understanding"]["scope"] == "broad"
        kinds = [s["kind"] for s in done["sources"] if s["episode_id"] == e1]
        assert kinds[0] == "summary"
        assert (
            "This question is broad" not in llm.prompts[-1]
        )  # it's in the system prompt

    def test_llm_failure_still_returns_sources(self, db):
        _library(db)

        class Failing(FakeLLM):
            def chat_stream(self, system, user, max_tokens=2000):
                raise RuntimeError("model crashed")

        done = self._asker(db, Failing()).ask("bank rates")
        assert done["mode"] == "llm_failed"
        assert "model crashed" in done["answer"]
        assert done["sources"]


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------


def _client(tmp_path, monkeypatch):
    from p3.api import deps

    monkeypatch.setattr(deps, "_DB_PATH", str(tmp_path / "api.duckdb"))
    monkeypatch.setattr(deps, "_CONFIG_PATH", str(tmp_path / "feeds.yaml"))
    monkeypatch.setenv("P3_DEV_USER", "tester")
    deps.close_db()
    from fastapi.testclient import TestClient

    from p3.api.main import app

    return TestClient(app), deps


def _events(body: str):
    return [
        json.loads(block.split("data: ", 1)[1])
        for block in body.strip().split("\n\n")
        if "data: " in block
    ]


class TestAskAPI:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        client, deps = _client(tmp_path, monkeypatch)
        from p3.api.routers import ask as ask_router

        db = deps.get_db()
        client.get("/api/me")  # tester: first user, admin
        _library(db, username="tester")

        def fake_asker(username):
            return Asker(
                db, Retriever(db, FakeEmbedder(), FakeReranker()), FakeLLM(), username
            )

        monkeypatch.setattr(ask_router, "_asker", fake_asker)
        yield client, db
        deps.close_db()
        if os.path.exists(tmp_path / "api.duckdb"):
            os.remove(tmp_path / "api.duckdb")

    def test_stream_stores_the_thread(self, api):
        client, db = api
        resp = client.post("/api/ask", json={"question": "What about bank rates?"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _events(resp.text)
        done = events[-1]
        assert done["event"] == "done" and "retrieval" not in done
        thread = client.get(f"/api/ask/threads/{done['thread_id']}").json()
        assert [m["role"] for m in thread["messages"]] == ["user", "assistant"]
        assert thread["messages"][1]["citations"][0]["cited"] is True
        stored = db.get_ask_thread(done["thread_id"], "tester")
        assert stored["messages"][1]["retrieval"]["mode"] == "answer"

        follow = client.post(
            "/api/ask",
            json={"question": "And inflation?", "thread_id": done["thread_id"]},
        )
        assert _events(follow.text)[-1]["thread_id"] == done["thread_id"]
        assert client.get("/api/ask/threads").json()[0]["message_count"] == 4

    def test_threads_are_private(self, api):
        client, _ = api
        done = _events(client.post("/api/ask", json={"question": "rates?"}).text)[-1]
        bob = {"Remote-User": "bob"}
        assert (
            client.get(f"/api/ask/threads/{done['thread_id']}", headers=bob).status_code
            == 404
        )
        assert client.get("/api/ask/threads", headers=bob).json() == []
        resp = client.post(
            "/api/ask",
            json={"question": "x", "thread_id": done["thread_id"]},
            headers=bob,
        )
        assert resp.status_code == 404
        resp = client.post(
            f"/api/ask/messages/{done['message_id']}/rating",
            json={"rating": -1},
            headers=bob,
        )
        assert resp.status_code == 404
        assert (
            client.delete(
                f"/api/ask/threads/{done['thread_id']}", headers=bob
            ).status_code
            == 404
        )

    def test_other_users_get_nothing_from_my_library(self, api):
        client, _ = api
        events = _events(
            client.post(
                "/api/ask",
                json={"question": "bank rates"},
                headers={"Remote-User": "bob"},
            ).text
        )
        assert events[-1]["sources"] == []
        assert events[-1]["mode"] == "not_covered"

    def test_rating_and_delete(self, api):
        client, db = api
        done = _events(client.post("/api/ask", json={"question": "rates?"}).text)[-1]
        resp = client.post(
            f"/api/ask/messages/{done['message_id']}/rating", json={"rating": 1}
        )
        assert resp.status_code == 200
        assert (
            db.get_ask_thread(done["thread_id"], "tester")["messages"][1]["rating"] == 1
        )
        assert (
            client.post(
                f"/api/ask/messages/{done['message_id']}/rating", json={"rating": 5}
            ).status_code
            == 422
        )
        assert client.delete(f"/api/ask/threads/{done['thread_id']}").status_code == 200
        assert client.get("/api/ask/threads").json() == []

    def test_paused_during_transcription(self, api, monkeypatch):
        client, _ = api
        from p3.api import tasks

        tasks.transcribing.set()
        try:
            done = _events(
                client.post("/api/ask", json={"question": "bank rates"}).text
            )[-1]
        finally:
            tasks.transcribing.clear()
        assert done["mode"] == "paused"
        assert client.get("/api/ask/status").json()["models_paused"] is False

    def test_admin_endpoints(self, api, monkeypatch):
        client, _ = api
        resp = client.get("/api/ask/retrieve", params={"q": "bank rates"})
        assert resp.status_code == 200 and resp.json()["passages"]
        assert (
            client.get(
                "/api/ask/retrieve",
                params={"q": "bank"},
                headers={"Remote-User": "bob"},
            ).status_code
            == 403
        )

        from p3.api.job_queue import job_runner

        queued = []
        monkeypatch.setattr(
            job_runner, "enqueue", lambda fn, *a: queued.append(fn.__name__)
        )
        assert client.post("/api/ask/index").status_code == 200
        assert queued == ["task_index"]


# ----------------------------------------------------------------------
# Evaluation
# ----------------------------------------------------------------------


class TestEvaluation:
    def test_relevance_by_episode_and_time(self):
        from p3.eval_ask import is_relevant

        item = {"relevant": [{"episode_id": 1, "start_s": 100.0, "end_s": 160.0}]}
        assert is_relevant(
            {"episode_id": 1, "start_s": 170, "end_s": 230, "kind": "transcript"}, item
        )
        assert not is_relevant(
            {"episode_id": 1, "start_s": 300, "end_s": 360, "kind": "transcript"}, item
        )
        assert not is_relevant(
            {"episode_id": 1, "start_s": None, "end_s": None, "kind": "summary"}, item
        )
        assert not is_relevant(
            {"episode_id": 2, "start_s": 100, "end_s": 160, "kind": "transcript"}, item
        )
        whole = {"relevant": [{"episode_id": 1}]}
        assert is_relevant(
            {"episode_id": 1, "start_s": None, "end_s": None, "kind": "summary"}, whole
        )

    def test_summarize_and_bootstrap(self):
        from p3.eval_ask import paired_bootstrap, summarize

        assert summarize([1, 3, None, 20], k=12) == {
            "n": 4,
            "recall": 0.5,
            "mrr": (1 + 1 / 3 + 1 / 20) / 4,
        }
        same = paired_bootstrap([0.5, 1.0, 0.0], [0.5, 1.0, 0.0])
        assert same == {"mean_diff": 0.0, "low": 0.0, "high": 0.0}
        better = paired_bootstrap([0.0] * 30, [1.0] * 30)
        assert better["low"] > 0
        with pytest.raises(ValueError):
            paired_bootstrap([], [])

    def test_evaluate_retrieval_reports_every_stage(self, db):
        from p3.eval_ask import evaluate_retrieval

        e1, e2 = _library(db)
        questions = [
            {
                "id": "q1",
                "question": "bank interest rates",
                "language": "en",
                "stratum": "lookup",
                "relevant": [{"episode_id": e1}],
            },
            {
                "id": "q2",
                "question": "phone camera",
                "language": "en",
                "stratum": "lookup",
                "relevant": [{"episode_id": e2, "start_s": 20.0, "end_s": 40.0}],
            },
            {
                "id": "q3",
                "question": "football scores",
                "language": "en",
                "stratum": "no_answer",
                "relevant": [],
            },
        ]
        result = evaluate_retrieval(
            db, Retriever(db, FakeEmbedder(), FakeReranker()), questions
        )
        assert result["overall"]["final"] == {"n": 2, "recall": 1.0, "mrr": 1.0}
        assert set(result["overall"]) == {
            "bm25",
            "vector",
            "fused",
            "reranked",
            "final",
        }
        assert "lookup" in result["strata"] and "lang:en" in result["strata"]
        assert len(result["questions"]) == 3


class TestShowNotes:
    def test_notes_text_strips_html(self):
        from p3.chunker import notes_text

        assert notes_text("<p>Guest: <b>Jane&nbsp;Doe</b></p><p>Links</p>") == (
            "Guest: Jane\xa0Doe\nLinks"
        )
        assert notes_text(None) == "" and len(notes_text("x" * 5000)) == 3000

    def test_notes_are_indexed_and_backfilled(self, db):
        pid = db.add_podcast("Pod", "http://example.com/rss")
        eid = db.add_episode(pid, "Ep", None, "http://x/1")
        db.add_transcript_segments(eid, _segments(["hello there"]))
        db.update_episode_status(eid, "processed")
        index_episode(db, eid, FakeEmbedder())
        assert db.episodes_to_index("fake-embed") == []

        assert db.set_missing_description("http://x/1", "Guest: Jane Doe, economist")
        assert not db.set_missing_description("http://x/1", "other notes")  # kept
        assert db.episodes_to_index("fake-embed") == [eid]
        index_episode(db, eid, FakeEmbedder())
        kinds = [c["kind"] for c in db.get_chunks(list(range(1, 20))).values()]
        assert "notes" in kinds
        assert db.episodes_to_index("fake-embed") == []
        assert db.bm25_search(tokenize("economist"), None)

    def test_new_episodes_keep_their_notes(self, db):
        pid = db.add_podcast("Pod", "http://example.com/rss")
        eid = db.add_episode(pid, "Ep", None, "http://x/2", description="Notes")
        assert db.get_episode_by_id(eid)["description"] == "Notes"


class TestQualityAPI:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        client, deps = _client(tmp_path, monkeypatch)
        from p3 import eval_ask
        from p3.api.job_queue import job_runner
        from p3.api.routers import ask as ask_router

        monkeypatch.setattr(eval_ask, "EVAL_DIR", tmp_path / "eval")
        monkeypatch.setattr(eval_ask, "QUESTIONS_FILE", tmp_path / "eval" / "q.json")
        self.queued = []
        monkeypatch.setattr(
            job_runner, "enqueue", lambda fn, *a: self.queued.append(fn.__name__)
        )
        db = deps.get_db()
        client.get("/api/me")
        _library(db, username="tester")
        monkeypatch.setattr(
            ask_router,
            "_asker",
            lambda u: Asker(
                db, Retriever(db, FakeEmbedder(), FakeReranker()), FakeLLM(), u
            ),
        )
        yield client, tmp_path
        deps.close_db()

    def test_rated_answers_go_to_the_eval_set_for_review(self, api):
        client, tmp_path = api
        done = _events(client.post("/api/ask", json={"question": "bank rates?"}).text)[
            -1
        ]
        client.post(
            f"/api/ask/messages/{done['message_id']}/rating", json={"rating": -1}
        )
        [rated] = client.get("/api/ask/feedback").json()
        assert rated["question"] == "bank rates?" and rated["rating"] == -1

        item = client.post(f"/api/ask/feedback/{done['message_id']}/to-eval").json()
        assert item["needs_review"] and item["candidates"]
        assert (
            client.post(f"/api/ask/feedback/{done['message_id']}/to-eval").status_code
            == 409
        )
        overview = client.get("/api/ask/eval").json()
        assert overview["questions"] == 1 and overview["scorable"] == 0
        assert (
            client.get("/api/ask/feedback", headers={"Remote-User": "bob"}).status_code
            == 403
        )

    def test_eval_jobs_are_queued(self, api):
        client, _ = api
        assert client.post("/api/ask/eval/run").status_code == 400  # no questions yet
        assert client.post("/api/ask/eval/generate?count=10").status_code == 200
        assert self.queued == ["task_eval_generate"]
