"""End-to-end smoke test: ``p3 fetch`` -> ``transcribe`` -> ``digest`` ->
``export`` on a two-episode sample feed, through the real CLI.

Everything of P3's own runs for real (feed parsing, downloading, the
database, speaker alignment and naming, the digest, its translation,
export). Only what lies outside it is a stand-in: HTTP (requests), ffmpeg,
the speech and diarization models, and the LLM.
"""

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from p3 import cli
from p3.database import P3Database

FEED_URL = "https://podcast.example.com/feed.xml"

FEED = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Humans &amp; Machines</title>
  <link>https://podcast.example.com/</link>
  <description>Conversations about people and AI.</description>
  <item>
    <title>Lisa Carlin on strategy &lt;execution&gt;</title>
    <guid>ep-2</guid>
    <pubDate>Tue, 10 Nov 2026 09:00:00 GMT</pubDate>
    <description>Ross Dawson talks with Lisa Carlin.</description>
    <enclosure url="https://cdn.example.com/ep2.mp3" length="1" type="audio/mpeg"/>
  </item>
  <item>
    <title>Davide Dell'Anna on hybrid intelligence</title>
    <guid>ep-1</guid>
    <pubDate>Tue, 03 Nov 2026 09:00:00 GMT</pubDate>
    <description>Ross Dawson talks with Davide Dell'Anna.</description>
    <enclosure url="https://cdn.example.com/ep1.mp3" length="1" type="audio/mpeg"/>
  </item>
</channel></rss>
"""


class FakeResponse:
    def __init__(self, content: bytes):
        self.content = content
        self.headers = {"content-type": "application/rss+xml"}
        self.status_code = 200

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        yield self.content


def fake_request(method, url, **kwargs):
    if url == FEED_URL:
        return FakeResponse(FEED.encode())
    if url.startswith("https://cdn.example.com/"):
        return FakeResponse(b"fake audio of " + url.encode())
    raise AssertionError(f"unexpected request to {url}")


class FakeRun:
    """ffmpeg: copies its input to its output (the last argument)."""

    def __init__(self):
        self.commands = []

    def __call__(self, cmd, **kwargs):
        self.commands.append(cmd)
        assert cmd[0] == "ffmpeg"
        shutil.copyfile(cmd[cmd.index("-i") + 1], cmd[-1])

        class Done:
            returncode = 0
            stderr = ""

        return Done()


def _words(start, text, step=0.5):
    return [
        {"start": start + i * step, "end": start + (i + 1) * step, "text": f" {w}"}
        for i, w in enumerate(text.split())
    ]


HOST_LINE = "Welcome to the show, I'm Ross Dawson."
GUEST_LINE = "Thanks Ross, glad to be here, I'm the guest."


def fake_whisper(self, audio_path, language=None):
    """One segment spoken by two voices, with word timings."""
    words = _words(0, HOST_LINE) + _words(4, GUEST_LINE)
    return {
        "segments": [
            {
                "start": 0,
                "end": 9,
                "text": f"{HOST_LINE} {GUEST_LINE}",
                "speaker": None,
                "confidence": None,
                "words": words,
            }
        ],
        "language": "en",
    }


class FakeDiarizer:
    def __init__(self):
        self.released = 0

    def diarize(self, audio_path):
        turns = [
            {"start": 0, "end": 4, "speaker": "SPEAKER_00"},
            {"start": 4, "end": 9, "speaker": "SPEAKER_01"},
        ]
        return turns, {"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]}

    def release(self):
        self.released += 1


SUMMARY = (
    "**The main argument**\nHybrid teams beat **either** side alone.\n\n"
    "**Takeaways**\n- Design the team, not the tool\n- Measure outcomes"
)


def fake_chat(self, system, user, max_tokens=2000):
    """The LLM, answering each kind of request the digest makes."""
    if "identify the speakers" in system:
        return json.dumps(
            {
                "SPEAKER_00": {"name": "Ross Dawson", "role": "host"},
                "SPEAKER_01": {"name": None, "role": "guest"},
            }
        )
    if "Return valid JSON only" in system:
        return "Here you go:\n" + json.dumps(
            {
                "key_topics": ["Hybrid intelligence"],
                "themes": ["Human-AI teams"],
                "quotes": ["Design the team, not the tool"],
                "startups": ["Acme <AI>"],
            }
        )
    if "concise, substantive summaries" in system:
        assert "Ross Dawson: Welcome to the show" in user  # named transcript
        return SUMMARY
    if "translator" in system:
        if "Translate each line below" in user:
            items = user.split("Lines:\n", 1)[1].splitlines()
            return "\n".join(f"FR {item}" for item in items)
        return "FR " + user.split("Text:\n", 1)[1]
    raise AssertionError(f"unexpected LLM request: {system[:60]}")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("P3_HOME", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "feeds.yaml").write_text(
        yaml.safe_dump(
            {
                "feeds": [{"name": "Humans & Machines", "url": FEED_URL}],
                "settings": {
                    "max_episodes_per_feed": 2,
                    "parakeet_enabled": False,
                    "llm_provider": "ollama",
                    "llm_model": "test-model",
                },
            }
        )
    )
    return tmp_path


@pytest.fixture
def stand_ins(monkeypatch):
    run = FakeRun()
    diarizer = FakeDiarizer()
    released = []
    monkeypatch.setattr("p3.downloader.requests.request", fake_request)
    monkeypatch.setattr("p3.downloader.subprocess.run", run)
    monkeypatch.setattr("p3.cli.make_diarizer", lambda settings: diarizer)
    monkeypatch.setattr(
        "p3.cli.release_ollama_models", lambda url=None: released.append(url)
    )
    monkeypatch.setattr(
        "p3.transcriber.AudioTranscriber.detect_language", lambda self, path: "en"
    )
    monkeypatch.setattr(
        "p3.transcriber.AudioTranscriber.transcribe_with_whisper", fake_whisper
    )
    monkeypatch.setattr("p3.transcriber.AudioTranscriber.unload_models", lambda s: 0)
    monkeypatch.setattr("p3.llm.LLMClient.chat", fake_chat)
    return {"run": run, "diarizer": diarizer, "released": released}


def _p3(home: Path, *args: str) -> str:
    """Run one ``p3`` command and return its output; fails on an error."""
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


def _episodes(home: Path):
    db = P3Database(str(home / "data" / "p3.duckdb"))
    try:
        episodes = sorted(db.get_all_episodes(), key=lambda e: e["guid"])
        return db, episodes
    except Exception:
        db.close()
        raise


def test_fetch_transcribe_digest_export(home, stand_ins):
    # fetch: the config feed is imported and both episodes downloaded and
    # normalized to 16 kHz mono.
    out = _p3(home, "fetch")
    assert "Total downloaded: 2 episodes" in out
    db, episodes = _episodes(home)
    try:
        assert [e["status"] for e in episodes] == ["downloaded", "downloaded"]
        assert [e["guid"] for e in episodes] == ["ep-1", "ep-2"]
        assert all(Path(e["file_path"]).exists() for e in episodes)
        assert episodes[0]["description"] == "Ross Dawson talks with Davide Dell'Anna."
    finally:
        db.close()
    ffmpeg = stand_ins["run"].commands[0]
    assert ffmpeg[ffmpeg.index("-ar") + 1] == "16000"
    assert ffmpeg[ffmpeg.index("-ac") + 1] == "1"

    # A second fetch finds nothing new.
    assert "Total downloaded: 0 episodes" in _p3(home, "fetch")

    # transcribe: Ollama released first, the segment split by speaker, each
    # speaker's voice kept.
    assert "Transcribed 2 of 2" in _p3(home, "transcribe")
    assert stand_ins["released"]
    assert stand_ins["diarizer"].released == 2
    db, episodes = _episodes(home)
    try:
        assert [e["status"] for e in episodes] == ["transcribed", "transcribed"]
        segments = db.get_transcripts_for_episode(episodes[0]["id"])
        assert [(s["speaker"], s["text"]) for s in segments] == [
            ("SPEAKER_00", HOST_LINE),
            ("SPEAKER_01", GUEST_LINE),
        ]
        assert set(db.get_speaker_voices(episodes[0]["id"])) == {
            "SPEAKER_00",
            "SPEAKER_01",
        }
    finally:
        db.close()

    # digest: speakers named, summary and its French version stored, audio
    # deleted.
    assert "Processed 2 of 2" in _p3(home, "digest")
    db, episodes = _episodes(home)
    try:
        assert [e["status"] for e in episodes] == ["processed", "processed"]
        assert not any(Path(e["file_path"] or "missing").exists() for e in episodes)
        speakers = db.get_episode_speakers(episodes[0]["id"])
        assert speakers["SPEAKER_00"]["name"] == "Ross Dawson"
        summary = db.get_summary_by_episode(episodes[0]["id"])
        assert summary["full_summary"] == SUMMARY
        assert summary["full_summary_fr"].startswith("FR ")
        assert summary["key_topics"] == ["Hybrid intelligence"]
        assert summary["key_topics_fr"] == ["FR Hybrid intelligence"]
        assert summary["startups"] == ["Acme <AI>"]
    finally:
        db.close()

    # export: today's digest in every format.
    _p3(home, "export", "--format", "markdown", "--format", "json", "--format", "html")
    day = datetime.now().strftime("%Y-%m-%d")
    files = {p.suffix: p for p in home.rglob(f"digest_{day}.*")}
    assert set(files) == {".md", ".json", ".html"}
    markdown = files[".md"].read_text()
    assert "Lisa Carlin on strategy <execution>" in markdown
    assert "Hybrid teams beat" in markdown
    digest = json.loads(files[".json"].read_text())
    assert "Davide Dell'Anna on hybrid intelligence" in json.dumps(digest)
    html = files[".html"].read_text()
    assert "Lisa Carlin on strategy &lt;execution&gt;" in html
    assert "<strong>The main argument</strong>" in html
    assert "Acme &lt;AI&gt;" in html

    assert re.search(r"Processed\W+2\b", _p3(home, "status"))
