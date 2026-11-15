"""The transcription backends on what the real engines returned.

mlx-whisper, Parakeet and faster-whisper each ran once on short clips made
with macOS's ``say`` (English in two voices, French in one), called the way
p3/transcriber.py calls them; their raw output is in tests/fixtures/speech/
(tests/fixtures/speech/record.py records it again). Here each engine's entry
point gives that output back, and the episode goes through
``AudioTranscriber.transcribe_episode`` into a real database, split by
speaker where a diarizer says the voice changes. Language detection itself
is not replayed: it runs MLX code on the audio.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from p3 import transcriber as transcriber_module
from p3.database import P3Database
from p3.settings import DEFAULT_PARAKEET_MODEL
from p3.transcriber import WHISPER_MODELS, AudioTranscriber

SPEECH = Path(__file__).parent / "fixtures" / "speech"
CLIPS = json.loads((SPEECH / "clips.json").read_text())


def _recorded(name: str):
    return json.loads((SPEECH / f"{name}.json").read_text())


@pytest.fixture
def db(tmp_path):
    database = P3Database(str(tmp_path / "test.duckdb"))
    yield database
    database.close()


@pytest.fixture
def episode_id(db, tmp_path):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"the clip")  # only its path reaches the engine
    pid = db.add_podcast("Workbench Radio", "https://workbench.example/feed")
    return db.add_episode(
        pid, "Ep", datetime.now(), "https://workbench.example/ep.mp3", str(audio)
    )


@pytest.fixture
def engines(monkeypatch):
    """Every engine missing, as on a machine without them; each test puts
    back the one it replays. Returns the calls each engine got."""
    calls: dict = {}
    for flag in ("WHISPER_AVAILABLE", "PARAKEET_AVAILABLE", "FASTER_WHISPER_AVAILABLE"):
        monkeypatch.setattr(transcriber_module, flag, False)
    # What unload_models touches when MLX is there; with Parakeet alone it
    # imports mlx.core itself, which isn't installed off the Mac.
    mx = SimpleNamespace(clear_cache=lambda: None)
    monkeypatch.setattr(transcriber_module, "mx", mx, raising=False)
    monkeypatch.setitem(sys.modules, "mlx", SimpleNamespace(core=mx))
    monkeypatch.setitem(sys.modules, "mlx.core", mx)
    monkeypatch.setattr(
        transcriber_module,
        "ModelHolder",
        SimpleNamespace(model=None, model_path=None),
        raising=False,
    )
    return calls


def _detects(monkeypatch, language):
    monkeypatch.setattr(AudioTranscriber, "detect_language", lambda self, p: language)


def _mlx_whisper(monkeypatch, calls, name):
    monkeypatch.setattr(transcriber_module, "WHISPER_AVAILABLE", True)

    def transcribe(path, **options):
        calls.setdefault("mlx_whisper", []).append(options)
        return _recorded(name)

    monkeypatch.setattr(
        transcriber_module,
        "mlx_whisper",
        SimpleNamespace(transcribe=transcribe),
        raising=False,
    )


def _parakeet(monkeypatch, calls):
    """parakeet-mlx's AlignedResult, rebuilt from the recording with the
    attributes P3 reads."""
    recorded = _recorded("parakeet_en")
    result = SimpleNamespace(
        sentences=[
            SimpleNamespace(
                text=s["text"],
                start=s["start"],
                end=s["end"],
                confidence=s["confidence"],
                tokens=[SimpleNamespace(**t) for t in s["tokens"]],
            )
            for s in recorded["sentences"]
        ]
    )

    class Model:
        def transcribe(self, path, **options):
            calls.setdefault("parakeet", []).append(options)
            return result

    def from_pretrained(name):
        calls.setdefault("parakeet_model", []).append(name)
        return Model()

    monkeypatch.setattr(transcriber_module, "PARAKEET_AVAILABLE", True)
    monkeypatch.setattr(
        transcriber_module, "parakeet_from_pretrained", from_pretrained, raising=False
    )


def _faster_whisper(monkeypatch, calls):
    recorded = _recorded("faster_whisper_fr")

    class Model:
        def __init__(self, name, **options):
            calls.setdefault("faster_whisper_model", []).append((name, options))

        def transcribe(self, path, language=None):
            calls.setdefault("faster_whisper", []).append(language)
            segments = (SimpleNamespace(**s) for s in recorded["segments"])
            return segments, SimpleNamespace(language=recorded["language"])

    monkeypatch.setattr(transcriber_module, "FASTER_WHISPER_AVAILABLE", True)
    monkeypatch.setattr(transcriber_module, "FasterWhisperModel", Model, raising=False)


class SwitchDiarizer:
    """The two voices of the English clip: the host, then the guest from
    where the guest's recording starts."""

    def diarize(self, path):
        switch = CLIPS["switch"]
        turns = [
            {"start": 0.0, "end": switch, "speaker": "SPEAKER_00"},
            {"start": switch, "end": switch + 30, "speaker": "SPEAKER_01"},
        ]
        return turns, {"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]}

    def release(self):
        pass


def _said_by(db, episode_id):
    """Each speaker's words, joined, from the stored transcript."""
    said: dict = {}
    for s in db.get_transcripts_for_episode(episode_id):
        said.setdefault(s["speaker"], []).append(s["text"])
    return {speaker: " ".join(texts) for speaker, texts in said.items()}


def test_mlx_whisper_english_split_by_speaker(db, episode_id, engines, monkeypatch):
    _detects(monkeypatch, "en")
    _mlx_whisper(monkeypatch, engines, "mlx_whisper_en")

    AudioTranscriber(db, diarizer=SwitchDiarizer()).transcribe_episode(episode_id)

    (options,) = engines["mlx_whisper"]
    assert options["path_or_hf_repo"] == WHISPER_MODELS["turbo"]
    assert options["language"] == "en" and options["word_timestamps"] is True
    episode = db.get_episode_by_id(episode_id)
    assert episode["status"] == "transcribed" and episode["language"] == "en"
    host, guest = CLIPS["english"]
    assert _said_by(db, episode_id) == {"SPEAKER_00": host, "SPEAKER_01": guest}
    segments = db.get_transcripts_for_episode(episode_id)
    assert all(0 < s["confidence"] <= 1 for s in segments)
    assert set(db.get_speaker_voices(episode_id)) == {"SPEAKER_00", "SPEAKER_01"}


def test_parakeet_english_split_by_speaker(db, episode_id, engines, monkeypatch):
    _detects(monkeypatch, "en")
    _parakeet(monkeypatch, engines)

    AudioTranscriber(
        db, use_parakeet=True, diarizer=SwitchDiarizer()
    ).transcribe_episode(episode_id)

    assert engines["parakeet_model"] == [DEFAULT_PARAKEET_MODEL]
    (options,) = engines["parakeet"]
    assert options == {"chunk_duration": 600, "overlap_duration": 15}
    host, guest = CLIPS["english"]
    assert _said_by(db, episode_id) == {"SPEAKER_00": host, "SPEAKER_01": guest}
    assert db.get_episode_by_id(episode_id)["language"] == "en"


def test_mlx_whisper_french_is_told_the_language(db, episode_id, engines, monkeypatch):
    _detects(monkeypatch, "fr")
    _parakeet(monkeypatch, engines)  # installed, but not for French
    _mlx_whisper(monkeypatch, engines, "mlx_whisper_fr")

    AudioTranscriber(db, use_parakeet=True).transcribe_episode(episode_id)

    assert "parakeet" not in engines
    assert [o["language"] for o in engines["mlx_whisper"]] == ["fr"]
    (segment,) = db.get_transcripts_for_episode(episode_id)
    assert segment["text"] == CLIPS["french"] and segment["speaker"] is None
    assert db.get_episode_by_id(episode_id)["language"] == "fr"


def test_faster_whisper_detects_french_itself(db, episode_id, engines, monkeypatch):
    """Off the Mac there is no MLX language detection: faster-whisper is
    given no language and reports the one it heard."""
    _faster_whisper(monkeypatch, engines)

    transcriber = AudioTranscriber(db, whisper_model="small")
    transcriber.transcribe_episode(episode_id)

    assert engines["faster_whisper_model"] == [
        ("small", {"device": "auto", "compute_type": "auto"})
    ]
    assert engines["faster_whisper"] == [None]
    (segment,) = db.get_transcripts_for_episode(episode_id)
    assert segment["text"] == CLIPS["french"]
    assert 0.99 < segment["confidence"] <= 1
    assert db.get_episode_by_id(episode_id)["language"] == "fr"
