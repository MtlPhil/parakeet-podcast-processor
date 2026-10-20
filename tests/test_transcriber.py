"""Tests for AudioTranscriber. No model is loaded: language detection and
both transcription engines are monkeypatched."""

from datetime import datetime

import pytest

from p3.database import P3Database
from p3.transcriber import WHISPER_MODELS, AudioTranscriber

_SEGMENT = {
    "start": 0.0,
    "end": 1.0,
    "text": "Bonjour",
    "speaker": None,
    "confidence": 1.0,
}


@pytest.fixture
def db(tmp_path):
    database = P3Database(str(tmp_path / "test.duckdb"))
    yield database
    database.close()


@pytest.fixture
def episode_id(db, tmp_path):
    audio = tmp_path / "ep.wav"
    audio.write_bytes(b"audio")
    pid = db.add_podcast("Pod", "http://example.com/rss")
    return db.add_episode(
        pid, "Ep", datetime.now(), "http://example.com/ep.mp3", str(audio)
    )


def _fake_engines(transcriber, monkeypatch, detected):
    """Detect ``detected``; record which engine ran and with what language."""
    calls = []

    def engine(name):
        def run(path, language=None):
            calls.append((name, language))
            return {"segments": [_SEGMENT], "language": language or "fr"}

        return run

    monkeypatch.setattr(transcriber, "detect_language", lambda path: detected)
    monkeypatch.setattr(transcriber, "transcribe_with_parakeet", engine("parakeet"))
    monkeypatch.setattr(transcriber, "transcribe_with_whisper", engine("whisper"))
    return calls


class TestEngineByLanguage:
    def test_english_goes_to_parakeet(self, db, episode_id, monkeypatch):
        transcriber = AudioTranscriber(db, use_parakeet=True)
        calls = _fake_engines(transcriber, monkeypatch, "en")
        assert transcriber.transcribe_episode(episode_id)
        assert calls == [("parakeet", "en")]

    def test_french_goes_to_whisper_told_the_language(
        self, db, episode_id, monkeypatch
    ):
        transcriber = AudioTranscriber(db, use_parakeet=True)
        calls = _fake_engines(transcriber, monkeypatch, "fr")
        assert transcriber.transcribe_episode(episode_id)
        assert calls == [("whisper", "fr")]

    def test_failed_detection_goes_to_whisper_to_detect_itself(
        self, db, episode_id, monkeypatch
    ):
        transcriber = AudioTranscriber(db, use_parakeet=True)
        calls = _fake_engines(transcriber, monkeypatch, None)
        assert transcriber.transcribe_episode(episode_id)
        assert calls == [("whisper", None)]

    def test_parakeet_disabled_uses_whisper_for_english(
        self, db, episode_id, monkeypatch
    ):
        transcriber = AudioTranscriber(db, use_parakeet=False)
        calls = _fake_engines(transcriber, monkeypatch, "en")
        assert transcriber.transcribe_episode(episode_id)
        assert calls == [("whisper", "en")]

    def test_parakeet_languages_setting(self, db, episode_id, monkeypatch):
        transcriber = AudioTranscriber(
            db, use_parakeet=True, parakeet_languages=["en", "fr"]
        )
        calls = _fake_engines(transcriber, monkeypatch, "fr")
        assert transcriber.transcribe_episode(episode_id)
        assert calls == [("parakeet", "fr")]


class TestLanguageStored:
    def test_detected_language_is_stored(self, db, episode_id, monkeypatch):
        transcriber = AudioTranscriber(db, use_parakeet=True)
        _fake_engines(transcriber, monkeypatch, "fr")
        assert transcriber.transcribe_episode(episode_id)
        episode = db.get_episode_by_id(episode_id)
        assert episode["status"] == "transcribed"
        assert episode["language"] == "fr"

    def test_unknown_language_is_left_unset(self, db, episode_id, monkeypatch):
        transcriber = AudioTranscriber(db)
        monkeypatch.setattr(transcriber, "detect_language", lambda path: None)
        monkeypatch.setattr(
            transcriber,
            "transcribe_with_whisper",
            lambda path, language=None: {"segments": [_SEGMENT], "language": None},
        )
        assert transcriber.transcribe_episode(episode_id)
        assert db.get_episode_by_id(episode_id)["language"] is None


class TestDetectLanguage:
    def test_failure_returns_none_instead_of_guessing(self, db, monkeypatch):
        transcriber = AudioTranscriber(db)

        def boom():
            raise RuntimeError("no model")

        monkeypatch.setattr(transcriber, "_load_whisper", boom)
        assert transcriber.detect_language("missing.wav") is None


class TestWhisperModelSetting:
    def test_short_names_map_to_mlx_repos(self):
        assert AudioTranscriber(None, whisper_model="turbo").whisper_model == (
            WHISPER_MODELS["turbo"]
        )
        assert AudioTranscriber(None, whisper_model="base").whisper_model == (
            "mlx-community/whisper-base-mlx"
        )

    def test_other_values_are_used_as_repos(self):
        repo = "someone/whisper-custom"
        assert AudioTranscriber(None, whisper_model=repo).whisper_model == repo

    def test_default_is_turbo(self):
        assert AudioTranscriber(None).whisper_model == WHISPER_MODELS["turbo"]
