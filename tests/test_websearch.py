"""The web as Ask's second source: Gemini grounding and SearXNG results as
passages, the [W1] citations, and the Ask API with web search (stand-ins
only: no network)."""

import os

import pytest

from p3 import websearch
from p3.ask import Asker, validate_web_citations
from p3.database import P3Database
from p3.llm import LLMClient
from p3.retriever import Retriever
from tests.test_ask import (
    FakeEmbedder,
    FakeLLM,
    FakeReranker,
    _client,
    _events,
    _library,
)
from tests.test_llm import _Recorder

GROUNDED = {
    "candidates": [
        {
            "content": {"parts": [{"text": "Rates fell. Reuters says so."}]},
            "groundingMetadata": {
                "groundingChunks": [
                    {"web": {"uri": "https://redirect/1", "title": "reuters.com"}},
                    {"web": {"uri": "https://redirect/2", "title": "lapresse.ca"}},
                    {"web": {"title": "no link"}},
                ],
                "groundingSupports": [
                    {
                        "segment": {"text": "Rates fell."},
                        "groundingChunkIndices": [0, 1],
                    },
                    {
                        "segment": {"text": "Reuters says so."},
                        "groundingChunkIndices": [0],
                    },
                    {"segment": {"text": "Unlinked."}, "groundingChunkIndices": [2, 7]},
                ],
            },
        }
    ]
}


@pytest.fixture
def db(tmp_path):
    database = P3Database(str(tmp_path / "test.duckdb"))
    yield database
    database.close()


class FakeWeb:
    name = "fake"

    def __init__(self, passages=None, fail=False):
        self.passages = (
            passages
            if passages is not None
            else [
                {
                    "title": "Bank of Canada cuts",
                    "url": "https://news.example.com/boc",
                    "site": "news.example.com",
                    "text": "The Bank of Canada cut its rate in November.",
                }
            ]
        )
        self.fail = fail
        self.asked = []

    def search(self, question, language="en"):
        self.asked.append((question, language))
        if self.fail:
            raise RuntimeError("search down")
        return self.passages


def test_grounded_reply_becomes_one_passage_per_page():
    passages = websearch.grounded_passages(GROUNDED)
    assert [(p["site"], p["text"]) for p in passages] == [
        ("reuters.com", "Rates fell. Reuters says so."),
        ("lapresse.ca", "Rates fell."),
    ]
    assert websearch.grounded_passages({"candidates": [{}]}) == []


def test_gemini_search_asks_for_google_search(monkeypatch):
    recorder = _Recorder(monkeypatch, body=GROUNDED)
    client = LLMClient(provider="gemini", model="gemini-x", api_key="k")
    passages = websearch.GeminiWebSearch(client).search("taux?", "fr")
    assert recorder.json["tools"] == [{"google_search": {}}]
    assert "French" in recorder.json["systemInstruction"]["parts"][0]["text"]
    assert len(passages) == 2


def test_a_grounding_quota_fails_at_once_and_says_so(monkeypatch):
    _Recorder(monkeypatch, status=429, body={})
    slept = []
    monkeypatch.setattr("p3.llm.time.sleep", slept.append)
    client = LLMClient(provider="gemini", model="gemini-x", api_key="k")
    with pytest.raises(RuntimeError, match="over its quota"):
        websearch.GeminiWebSearch(client).search("taux?")
    assert slept == [] and client.model == "gemini-x"  # no retry, no fallback


def test_searxng_results_become_passages(monkeypatch):
    seen = {}

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "results": [
                    {
                        "url": "https://www.lapresse.ca/a",
                        "title": "A",
                        "content": "Le taux  baisse.",
                    },
                    {"url": "https://x.example/b", "title": "B", "content": ""},
                ]
            }

    def get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params, headers=headers)
        return Response()

    monkeypatch.setattr(websearch.httpx, "get", get)
    (p,) = websearch.SearxngWebSearch("http://searx.local/").search("taux", "fr")
    assert seen["url"] == "http://searx.local/search"
    assert seen["params"]["format"] == "json"
    assert (p["site"], p["text"]) == ("lapresse.ca", "Le taux baisse.")
    assert "text/html" in seen["headers"]["Accept"]  # SearXNG's limiter wants it
    assert seen["headers"]["Accept-Language"] == "fr"


@pytest.mark.parametrize("status,said", [(403, "search.formats"), (429, "limiter")])
def test_searxng_refusals_say_what_to_change(monkeypatch, status, said):
    class Refused:
        status_code = status

    monkeypatch.setattr(websearch.httpx, "get", lambda *a, **k: Refused())
    with pytest.raises(RuntimeError, match=said):
        websearch.SearxngWebSearch("http://s").search("q")


def test_the_setting_picks_the_searcher(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert websearch.make_web_searcher({}) is None
    assert websearch.make_web_searcher({"web_search": "gemini"}).name == "gemini"
    assert websearch.make_web_searcher({"web_search": "searxng"}) is None  # no URL
    searx = websearch.make_web_searcher(
        {"web_search": "searxng", "searxng_url": "http://s"}
    )
    assert searx.name == "searxng"


def test_web_citations_are_checked_apart():
    checked = validate_web_citations("A [1]. B [W1]. C [w 2]. D [W9].", 2)
    assert checked["answer"] == "A [1]. B [W1]. C [W2]. D ."
    assert checked["cited"] == [1, 2]


class TestAnswerWithTheWeb:
    def _asker(self, db, llm, web, scores=None):
        return Asker(
            db,
            Retriever(db, FakeEmbedder(), FakeReranker(scores)),
            llm,
            None,
            web_searcher=web,
        )

    def test_web_passages_are_numbered_apart_and_cited(self, db):
        _library(db)
        llm = FakeLLM(answer="Rates rose [1]. Then a cut [W1]. Bad [W4].")
        web = FakeWeb()
        events = list(self._asker(db, llm, web).answer_stream("Bank rates?", web=True))
        sources = next(e for e in events if e["event"] == "sources")
        assert sources["web_sources"][0]["url"] == "https://news.example.com/boc"
        done = events[-1]
        assert done["answer"] == "Rates rose [1]. Then a cut [W1]. Bad ."
        assert done["web_sources"][0]["cited"] is True
        assert done["sources"][0]["cited"] is True
        prompt = llm.prompts[-1]
        assert "Web passages:\n\n[W1] news.example.com: Bank of Canada cuts" in prompt
        assert web.asked == [("Bank rates?", "en")]

    def test_without_the_switch_the_web_is_not_searched(self, db):
        _library(db)
        web, llm = FakeWeb(), FakeLLM()
        done = self._asker(db, llm, web).ask("Bank rates?")
        assert web.asked == [] and done["web_sources"] == []
        assert "Web passages" not in llm.prompts[-1]

    def test_the_web_answers_what_the_library_doesnt_cover(self, db):
        _library(db)
        llm = FakeLLM(answer="Only the web covers it [W1].")
        events = list(
            self._asker(db, llm, FakeWeb(), scores=0.001).answer_stream(
                "Bank rates?", web=True
            )
        )
        assert events[-1]["mode"] == "answer"
        assert events[-1]["web_sources"][0]["cited"] is True

    def test_a_failed_search_still_answers_from_the_library(self, db):
        _library(db)
        done = list(
            self._asker(db, FakeLLM(), FakeWeb(fail=True)).answer_stream(
                "Bank rates?", web=True
            )
        )[-1]
        assert done["mode"] == "answer" and done["web_sources"] == []
        assert done["web_error"] == "search down"


def test_the_api_stores_web_sources_with_the_answer(tmp_path, monkeypatch):
    client, deps = _client(tmp_path, monkeypatch)
    from p3.api.routers import ask as ask_router

    db = deps.get_db()
    client.get("/api/me")
    _library(db, username="tester")
    monkeypatch.setattr(
        ask_router,
        "_asker",
        lambda username: Asker(
            db,
            Retriever(db, FakeEmbedder(), FakeReranker()),
            FakeLLM(answer="Up [1], then down [W1]."),
            username,
            web_searcher=FakeWeb(),
        ),
    )
    done = _events(
        client.post("/api/ask", json={"question": "rates?", "web": True}).text
    )[-1]
    assert done["web_sources"][0]["cited"] is True
    thread = client.get(f"/api/ask/threads/{done['thread_id']}").json()
    assert thread["messages"][1]["web_sources"][0]["site"] == "news.example.com"
    assert client.get("/api/ask/status").json()["web_search"] is None  # not set up
    deps.close_db()
    if os.path.exists(tmp_path / "api.duckdb"):
        os.remove(tmp_path / "api.duckdb")
