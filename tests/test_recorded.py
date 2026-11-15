"""Integration tests on recorded model answers.

The LLM and embedding requests below go out for real, through P3's own
request code and the ollama library down to httpx, and are answered from
recordings of a real model (tests/cassette.py): gemma4:12b and bge-m3
through Ollama, and Gemini. Record them again after changing what P3 asks:

    P3_RECORD=1 PYTHONPATH=. pytest tests/test_recorded.py

which needs Ollama running with both models pulled and a Gemini key. The
episode is made up (tests/fixtures/http/ holds what was sent and received).
"""

import json
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from p3 import cli
from p3.database import P3Database
from tests.cassette import RECORDING, Cassette

MODEL = "gemma4:12b"
GEMINI_MODEL = "gemini-3.6-flash"
OLLAMA = "http://localhost:11434"

NOTES = (
    "Maya Okafor talks with bicycle mechanic Tomás Reyes about fixing old "
    "bikes, the right to repair, and teaching kids to true a wheel."
)

# (speaker, text): about eight minutes of a made-up show, with a sponsor read.
LINES = [
    ("SPEAKER_00", "Welcome to Workbench Radio, I'm Maya Okafor."),
    ("SPEAKER_00", "My guest today runs a community bike shop in Montreal."),
    ("SPEAKER_01", "Thanks Maya, I'm Tomás Reyes, glad to be here."),
    ("SPEAKER_00", "Tomás, why do so many old bikes end up in the trash?"),
    (
        "SPEAKER_01",
        "Mostly because people think a rusty chain means the bike is dead. "
        "A chain costs twenty dollars and ten minutes.",
    ),
    (
        "SPEAKER_01",
        "The frame is the expensive part, and a steel frame from the eighties "
        "will outlive all of us.",
    ),
    ("SPEAKER_00", "So what stops people from fixing them?"),
    (
        "SPEAKER_01",
        "Parts. Newer bikes use proprietary parts that only the brand sells, "
        "and some of them need software to adjust the gears.",
    ),
    (
        "SPEAKER_00",
        "This episode is brought to you by Gearbox Insurance. Gearbox covers "
        "your bike against theft for five dollars a month. Visit gearbox dot "
        "example.",
    ),
    ("SPEAKER_00", "Back to it. Is that where the right to repair comes in?"),
    (
        "SPEAKER_01",
        "Exactly. If the manufacturer won't sell the part or share the manual, "
        "the bike becomes a disposable product. Repair laws would force them "
        "to publish manuals and sell parts for ten years.",
    ),
    (
        "SPEAKER_01",
        "Quebec already passed a law against planned obsolescence, and I think "
        "bikes should be next.",
    ),
    ("SPEAKER_00", "You also teach kids. What do they learn first?"),
    (
        "SPEAKER_01",
        "To fix a flat. Then to true a wheel: you spin it, watch where the rim "
        "wobbles and tighten the spokes on the other side, a quarter turn at a "
        "time.",
    ),
    (
        "SPEAKER_01",
        "A kid who trued their own wheel never looks at a broken thing the "
        "same way again.",
    ),
    ("SPEAKER_00", "That's a great line. What's one thing listeners can do?"),
    (
        "SPEAKER_01",
        "Before you buy a new bike, take your old one to a community shop. "
        "Nine times out of ten we get it riding for under fifty dollars.",
    ),
    ("SPEAKER_00", "Tomás Reyes, thank you. That's Workbench Radio for today."),
]


def _segments():
    segments, start = [], 0.0
    for speaker, text in LINES:
        length = max(4.0, len(text.split()) * 0.4)
        segments.append(
            {
                "start": start,
                "end": start + length,
                "text": text,
                "speaker": speaker,
                "confidence": None,
            }
        )
        start += length + 0.5
    return segments


SUMMARY = {
    "key_topics": ["Bicycle repair", "Right to repair"],
    "themes": ["Repair over replacement"],
    "quotes": ["A chain costs twenty dollars and ten minutes."],
    "startups": [],
    "full_summary": (
        "**Old bikes are worth fixing**\nTomás Reyes says most discarded bikes "
        "need a chain, not a replacement.\n\n**Takeaways**\n- Take an old bike "
        "to a community shop before buying a new one"
    ),
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("P3_HOME", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "feeds.yaml").write_text(
        yaml.safe_dump(
            {
                "feeds": [],
                "settings": {
                    "llm_provider": "ollama",
                    "llm_model": MODEL,
                    "ollama_base_url": OLLAMA,
                },
            }
        )
    )
    return tmp_path


def _seed(home: Path, digested: bool = False) -> int:
    """The made-up episode, transcribed (and digested, if asked)."""
    db = P3Database(str(home / "data" / "p3.duckdb"))
    try:
        pid = db.add_podcast("Workbench Radio", "https://workbench.example/feed")
        eid = db.add_episode(
            pid,
            "Tomás Reyes on fixing old bikes",
            datetime.now(),
            "https://workbench.example/ep1.mp3",
            description=NOTES,
            guid="ep1",
        )
        db.store_transcript(
            eid,
            _segments(),
            "en",
            {"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]},
        )
        if digested:
            db.add_summary(episode_id=eid, digest_date=datetime.now(), **SUMMARY)
            db.update_episode_status(eid, "processed")
        return eid
    finally:
        db.close()


def _p3(home: Path, *args: str) -> str:
    obj: dict = {}
    result = CliRunner().invoke(
        cli.main,
        ["--config", str(home / "config" / "feeds.yaml"), *args],
        obj=obj,
        catch_exceptions=False,
    )
    if "db" in obj:
        obj["db"].close()
    assert result.exit_code == 0, result.output
    return result.output


def _ollama_chats(tape: Cassette):
    return [r["body"]["json"] for r in tape.requests if r["url"].endswith("/api/chat")]


def _assert_ollama_requests(chats):
    """What every Ollama chat request carries (see LLMClient)."""
    assert chats
    for body in chats:
        assert body["model"] == MODEL
        assert body["think"] is False
        assert [m["role"] for m in body["messages"]] == ["system", "user"]
        assert body["options"]["num_predict"] > 0
        assert body["options"]["repeat_penalty"] == 1.3
        assert body["options"]["num_ctx"] >= 8192


def test_digest_and_transcript_export_through_the_cli(home):
    eid = _seed(home)

    with Cassette("ollama_digest") as tape:
        assert "Processed 1 of 1" in _p3(home, "digest")

    _assert_ollama_requests(_ollama_chats(tape))
    db = P3Database(str(home / "data" / "p3.duckdb"))
    try:
        episode = db.get_episode_by_id(eid)
        assert episode["status"] == "processed"
        speakers = db.get_episode_speakers(eid)
        assert speakers["SPEAKER_00"]["name"] == "Maya Okafor"
        assert speakers["SPEAKER_01"]["name"] == "Tomás Reyes"
        summary = db.get_summary_by_episode(eid)
        assert summary["key_topics"] and all(
            isinstance(t, str) and t for t in summary["key_topics"]
        )
        assert summary["themes"] and summary["quotes"]
        assert "Gearbox" not in summary["full_summary"]  # the ad is left out
        assert summary["full_summary_fr"]
        assert summary["full_summary_fr"] != summary["full_summary"]
        assert len(summary["key_topics_fr"]) == len(summary["key_topics"])
    finally:
        db.close()

    # export-transcript: no model, the stored transcript as a file.
    day = datetime.now().strftime("%Y-%m-%d")
    out = home / "transcripts"
    _p3(home, "export-transcript", "--date", day, "--output-dir", str(out))
    text = (out / f"transcript_{eid}.txt").read_text()
    assert "true a wheel" in text


def test_write_grades_and_saves_a_blog_post(home):
    _seed(home, digested=True)

    with Cassette("ollama_write") as tape:
        out = _p3(home, "write", "--topic", "Why old bikes deserve a repair")

    _assert_ollama_requests(_ollama_chats(tape))
    assert "Blog post generated!" in out
    (post,) = (home / "blog_posts").rglob("*.md")
    text = post.read_text()
    front = yaml.safe_load(text.split("---")[1])
    assert front["title"] == "Why old bikes deserve a repair"
    assert 0 < float(front["final_score"]) <= 100
    assert "Français (Québec)" in text


def test_linkedin_post_in_both_languages(home):
    from p3.writer import BlogWriter

    eid = _seed(home, digested=True)
    db = P3Database(str(home / "data" / "p3.duckdb"))
    try:
        summary = db.get_summary_by_episode(eid)
        writer = BlogWriter(db=db, llm_model=MODEL, ollama_base_url=OLLAMA)
        with Cassette("ollama_linkedin") as tape:
            result = writer.generate_linkedin_post(summary)
        path = writer.save_linkedin_post(result, str(home / "linkedin"))
    finally:
        db.close()

    _assert_ollama_requests(_ollama_chats(tape))
    assert result["english"] and result["french_quebec"]
    assert result["english"] != result["french_quebec"]
    assert "## English" in Path(path).read_text()


def test_ask_indexes_and_answers_with_citations(home):
    from p3.ask import Asker
    from p3.embedder import Embedder
    from p3.indexer import index_episode
    from p3.llm import LLMClient
    from p3.retriever import Retriever

    eid = _seed(home, digested=True)
    db = P3Database(str(home / "data" / "p3.duckdb"))
    try:
        embedder = Embedder(ollama_base_url=OLLAMA)
        llm = LLMClient("ollama", MODEL, ollama_base_url=OLLAMA, num_ctx=8192)
        with Cassette("ollama_ask") as tape:
            assert index_episode(db, eid, embedder) > 0
            done = Asker(db, Retriever(db, embedder), llm, None).ask(
                "What does Tomás Reyes say about the right to repair?"
            )
    finally:
        db.close()

    embeds = [r["body"]["json"] for r in tape.requests if r["url"].endswith("/embed")]
    assert embeds and all(body["model"] == "bge-m3" for body in embeds)
    vectors = [
        v
        for exchange in tape.exchanges
        if exchange["request"]["url"].endswith("/embed")
        for v in exchange["response"]["body"]["json"]["embeddings"]
    ]
    assert vectors and all(len(v) == 1024 for v in vectors)
    streamed = [
        r["body"]["json"]
        for r in tape.requests
        if r["url"].endswith("/api/chat") and r["body"]["json"].get("stream")
    ]
    assert len(streamed) == 1  # the answer; the question step is not streamed
    assert done["mode"] == "answer" and done["answer"]
    assert done["cited"] and not done["invalid_citations"]
    assert {s["episode_id"] for s in done["sources"]} == {eid}


def test_gemini_chat_stream_and_tools(monkeypatch):
    from p3 import llm as llm_module
    from p3.llm import LLMClient, tool_result

    monkeypatch.setattr(llm_module, "_gemini_skipped_until", {})
    client = LLMClient("gemini", GEMINI_MODEL, api_key=None if RECORDING else "key")
    count_tool = {
        "name": "count_mentions",
        "description": "How many episodes of the library mention a word.",
        "parameters": {
            "type": "object",
            "properties": {"word": {"type": "string"}},
            "required": ["word"],
        },
    }
    question = {
        "role": "user",
        "content": "How many episodes mention bicycles? Call count_mentions.",
    }
    with Cassette("gemini") as tape:
        reply = client.chat("Answer in one short sentence.", "What is a bicycle?")
        pieces = list(
            client.chat_stream("Answer in one short sentence.", "What is a wheel?")
        )
        turn = client.chat_tools(
            "You answer questions about a podcast library.", [question], [count_tool]
        )
        (call,) = turn.calls
        final = client.chat_tools(
            "You answer questions about a podcast library.",
            [
                question,
                {"role": "assistant", "turn": turn},
                tool_result(call, json.dumps({"episodes": 3})),
            ],
            [count_tool],
        )

    assert reply.strip()
    assert len(pieces) >= 1 and "".join(pieces).strip()
    assert call.name == "count_mentions" and call.arguments.get("word")
    assert not final.calls and "3" in final.text
    urls = [r["url"] for r in tape.requests]
    assert urls[1].endswith(":streamGenerateContent")
    assert all(GEMINI_MODEL in u for u in urls)
    # The tool's answer goes back after the model's own call, unchanged.
    contents = tape.requests[-1]["body"]["json"]["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "user"]
