"""Tests for AudioTranscriber. No model is loaded: language detection and
both transcription engines are monkeypatched."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from p3 import transcriber as transcriber_module
from p3.database import P3Database
from p3.transcriber import WHISPER_MODELS, AudioTranscriber, TranscriptionError

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
        transcriber.transcribe_episode(episode_id)
        assert calls == [("parakeet", "en")]

    def test_french_goes_to_whisper_told_the_language(
        self, db, episode_id, monkeypatch
    ):
        transcriber = AudioTranscriber(db, use_parakeet=True)
        calls = _fake_engines(transcriber, monkeypatch, "fr")
        transcriber.transcribe_episode(episode_id)
        assert calls == [("whisper", "fr")]

    def test_failed_detection_goes_to_whisper_to_detect_itself(
        self, db, episode_id, monkeypatch
    ):
        transcriber = AudioTranscriber(db, use_parakeet=True)
        calls = _fake_engines(transcriber, monkeypatch, None)
        transcriber.transcribe_episode(episode_id)
        assert calls == [("whisper", None)]

    def test_parakeet_disabled_uses_whisper_for_english(
        self, db, episode_id, monkeypatch
    ):
        transcriber = AudioTranscriber(db, use_parakeet=False)
        calls = _fake_engines(transcriber, monkeypatch, "en")
        transcriber.transcribe_episode(episode_id)
        assert calls == [("whisper", "en")]

    def test_parakeet_languages_setting(self, db, episode_id, monkeypatch):
        transcriber = AudioTranscriber(
            db, use_parakeet=True, parakeet_languages=["en", "fr"]
        )
        calls = _fake_engines(transcriber, monkeypatch, "fr")
        transcriber.transcribe_episode(episode_id)
        assert calls == [("parakeet", "fr")]


class TestLanguageStored:
    def test_detected_language_is_stored(self, db, episode_id, monkeypatch):
        transcriber = AudioTranscriber(db, use_parakeet=True)
        _fake_engines(transcriber, monkeypatch, "fr")
        transcriber.transcribe_episode(episode_id)
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
        transcriber.transcribe_episode(episode_id)
        assert db.get_episode_by_id(episode_id)["language"] is None


class TestAllOrNothing:
    """An episode that can't be transcribed in full fails with the reason
    and stays as it was, so it can be retried."""

    def _assert_untouched(self, db, episode_id):
        assert db.get_episode_by_id(episode_id)["status"] == "downloaded"
        assert db.get_transcripts_for_episode(episode_id) == []

    def test_an_engine_failure_leaves_the_episode_downloaded(
        self, db, episode_id, monkeypatch
    ):
        transcriber = AudioTranscriber(db)
        monkeypatch.setattr(transcriber, "detect_language", lambda path: "fr")

        def fail(path, language=None):
            raise TranscriptionError("Whisper failed: out of memory")

        monkeypatch.setattr(transcriber, "transcribe_with_whisper", fail)
        with pytest.raises(TranscriptionError, match="out of memory"):
            transcriber.transcribe_episode(episode_id)
        self._assert_untouched(db, episode_id)

    def test_audio_without_speech_is_an_error(self, db, episode_id, monkeypatch):
        transcriber = AudioTranscriber(db)
        monkeypatch.setattr(transcriber, "detect_language", lambda path: None)
        silent = {"segments": [{**_SEGMENT, "text": ""}], "language": None}
        monkeypatch.setattr(
            transcriber, "transcribe_with_whisper", lambda path, language=None: silent
        )
        with pytest.raises(TranscriptionError, match="No speech"):
            transcriber.transcribe_episode(episode_id)
        self._assert_untouched(db, episode_id)

    def test_missing_audio_is_an_error(self, db, episode_id, tmp_path):
        (tmp_path / "ep.wav").unlink()
        with pytest.raises(TranscriptionError, match="Audio file not found"):
            AudioTranscriber(db).transcribe_episode(episode_id)

    def test_an_episode_already_transcribed_is_refused(self, db, episode_id):
        db.update_episode_status(episode_id, "transcribed")
        with pytest.raises(TranscriptionError, match="already transcribed"):
            AudioTranscriber(db).transcribe_episode(episode_id)

    def test_no_engine_installed_says_so(self, db, monkeypatch):
        monkeypatch.setattr(transcriber_module, "WHISPER_AVAILABLE", False)
        monkeypatch.setattr(transcriber_module, "FASTER_WHISPER_AVAILABLE", False)
        with pytest.raises(TranscriptionError, match="No speech-to-text engine"):
            AudioTranscriber(db).transcribe_with_whisper("ep.wav")


class TestFasterWhisper:
    """The engine off Apple Silicon, with a stand-in for the model."""

    @pytest.fixture
    def engine(self, monkeypatch):
        loaded = []

        class Model:
            def __init__(self, name, **options):
                loaded.append((name, options))

            def transcribe(self, path, language=None):
                self.asked = language
                segments = (
                    SimpleNamespace(start=s, end=s + 1, text=t, no_speech_prob=0.25)
                    for s, t in ((0.0, " Bonjour. "), (1.0, " "))
                )
                return segments, SimpleNamespace(language=language or "fr")

        monkeypatch.setattr(transcriber_module, "WHISPER_AVAILABLE", False)
        monkeypatch.setattr(transcriber_module, "PARAKEET_AVAILABLE", False)
        monkeypatch.setattr(transcriber_module, "FASTER_WHISPER_AVAILABLE", True)
        monkeypatch.setattr(
            transcriber_module, "FasterWhisperModel", Model, raising=False
        )
        return loaded

    def test_an_episode_is_transcribed_in_the_language_it_detects(
        self, db, episode_id, engine
    ):
        # Parakeet is asked for but can't run here: Whisper does everything.
        transcriber = AudioTranscriber(db, whisper_model="large", use_parakeet=True)
        transcriber.transcribe_episode(episode_id)

        assert engine == [("large-v3", {"device": "auto", "compute_type": "auto"})]
        assert transcriber._faster_whisper.asked is None  # it detects, untold
        episode = db.get_episode_by_id(episode_id)
        assert (episode["status"], episode["language"]) == ("transcribed", "fr")
        segments = db.get_transcripts_for_episode(episode_id)
        assert [(s["text"], s["confidence"]) for s in segments] == [("Bonjour.", 0.75)]

    def test_the_model_is_loaded_once_and_unloaded(self, db, engine):
        transcriber = AudioTranscriber(db)
        transcriber.transcribe_with_whisper("a.wav")
        transcriber.transcribe_with_whisper("b.wav", "en")
        assert len(engine) == 1 and transcriber._faster_whisper.asked == "en"
        transcriber.unload_models()
        assert transcriber._faster_whisper is None

    def test_a_failure_while_reading_segments_fails_the_episode(
        self, db, episode_id, engine, monkeypatch
    ):
        def broken(self, path, language=None):
            def segments():
                yield SimpleNamespace(start=0, end=1, text="Hi", no_speech_prob=0)
                raise RuntimeError("decoder crashed")

            return segments(), SimpleNamespace(language="en")

        monkeypatch.setattr(transcriber_module.FasterWhisperModel, "transcribe", broken)
        with pytest.raises(TranscriptionError, match="decoder crashed"):
            AudioTranscriber(db).transcribe_episode(episode_id)
        assert db.get_episode_by_id(episode_id)["status"] == "downloaded"


class TestParakeet:
    """Parakeet itself is replaced by a stand-in model."""

    def _transcriber(self, db, monkeypatch, model):
        monkeypatch.setattr(transcriber_module, "PARAKEET_AVAILABLE", True)
        monkeypatch.setattr(
            transcriber_module,
            "parakeet_from_pretrained",
            lambda name: model,
            raising=False,
        )
        return AudioTranscriber(db, use_parakeet=True)

    def test_long_audio_is_chunked_with_overlap(self, db, monkeypatch):
        seen = {}

        def transcribe(path, **options):
            seen.update(options)
            sentence = SimpleNamespace(
                start=599.5, end=601.0, text=" Hi. ", confidence=0.9, tokens=[]
            )
            return SimpleNamespace(sentences=[sentence])

        model = SimpleNamespace(transcribe=transcribe)
        result = self._transcriber(db, monkeypatch, model).transcribe_with_parakeet(
            "ep.wav", "en"
        )
        # Overlapping chunks, merged by parakeet-mlx: no cut at a boundary.
        assert seen == {"chunk_duration": 600, "overlap_duration": 15}
        assert result == {
            "segments": [
                {
                    "start": 599.5,
                    "end": 601.0,
                    "text": "Hi.",
                    "speaker": None,
                    "confidence": 0.9,
                    "words": [],
                }
            ],
            "language": "en",
        }

    def test_a_failure_hands_the_whole_episode_to_whisper(self, db, monkeypatch):
        def transcribe(path, **options):
            raise RuntimeError("chunk 3 failed")

        transcriber = self._transcriber(
            db, monkeypatch, SimpleNamespace(transcribe=transcribe)
        )
        whisper = {"segments": [_SEGMENT], "language": "en"}
        monkeypatch.setattr(
            transcriber, "transcribe_with_whisper", lambda path, language=None: whisper
        )
        # Never a transcript with a part missing.
        assert transcriber.transcribe_with_parakeet("ep.wav", "en") is whisper
        assert transcriber._parakeet is None  # released before Whisper loads


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
