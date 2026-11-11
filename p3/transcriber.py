"""Audio transcription using Parakeet MLX or Whisper MLX (Apple Silicon).

Each episode's language is detected first. Parakeet transcribes the languages
it handles reliably (English by default); everything else goes to Whisper,
which is told the language. Parakeet has no language input: on spontaneous
non-English speech it drifts into English that reads like a translation.

An episode is transcribed completely or not at all: a failure raises
TranscriptionError and leaves the episode as it was, so it can be retried.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .database import P3Database

logger = logging.getLogger(__name__)

try:
    from parakeet_mlx import from_pretrained as parakeet_from_pretrained

    PARAKEET_AVAILABLE = True
except ImportError:
    PARAKEET_AVAILABLE = False

try:
    import mlx.core as mx
    import mlx_whisper
    from mlx_whisper.audio import (
        N_FRAMES,
        SAMPLE_RATE,
        load_audio,
        log_mel_spectrogram,
        pad_or_trim,
    )
    from mlx_whisper.transcribe import ModelHolder

    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False

DEFAULT_PARAKEET_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"

# Languages Parakeet transcribes (setting: parakeet_languages); every other
# language goes to Whisper.
DEFAULT_PARAKEET_LANGUAGES = ("en",)

# Short names for Whisper's MLX conversions on Hugging Face. Any other value
# of the whisper_model setting is used as a Hugging Face repo as-is.
WHISPER_MODELS = {
    "tiny": "mlx-community/whisper-tiny",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large": "mlx-community/whisper-large-v3-mlx",
    "turbo": "mlx-community/whisper-large-v3-turbo",
}
DEFAULT_WHISPER_MODEL = "turbo"

# Parakeet works through a long episode in chunks of this length, to bound
# memory use. Consecutive chunks overlap, and parakeet-mlx merges them on the
# words they share, so a sentence that straddles a boundary is not cut.
_CHUNK_SECONDS = 600
_CHUNK_OVERLAP_SECONDS = 15

# Language detection listens to this many seconds of audio, starting a minute
# in so that intro music doesn't decide the result.
_LANGUAGE_PROBE_SECONDS = 30
_LANGUAGE_PROBE_START = 60


class TranscriptionError(RuntimeError):
    """An episode could not be transcribed; the message says why."""


class AudioTranscriber:
    def __init__(
        self,
        db: P3Database,
        whisper_model: str = DEFAULT_WHISPER_MODEL,
        use_parakeet: bool = False,
        parakeet_model: str = DEFAULT_PARAKEET_MODEL,
        parakeet_languages: Optional[Iterable[str]] = None,
    ):
        self.db = db
        self.whisper_model = WHISPER_MODELS.get(whisper_model, whisper_model)
        self.use_parakeet = use_parakeet
        self.parakeet_model = parakeet_model
        self.parakeet_languages = set(
            DEFAULT_PARAKEET_LANGUAGES
            if parakeet_languages is None
            else parakeet_languages
        )
        # Loaded lazily; the library is untyped.
        self._parakeet: Any = None

    def _load_whisper(self) -> Any:
        """The Whisper model, loaded through mlx_whisper's own cache in the
        float16 that mlx_whisper.transcribe uses by default, so language
        detection and transcription share one copy."""
        logger.info("Loading Whisper model: %s", self.whisper_model)
        return ModelHolder.get_model(self.whisper_model, mx.float16)

    def _release_whisper(self):
        if WHISPER_AVAILABLE:
            ModelHolder.model = None
            ModelHolder.model_path = None

    def detect_language(self, audio_path: str) -> Optional[str]:
        """Detect the spoken language with Whisper's language identification.

        Returns an ISO 639-1 code such as "en" or "fr", or None if detection
        fails or Whisper isn't installed.
        """
        if not WHISPER_AVAILABLE:
            return None
        try:
            model = self._load_whisper()
            audio = load_audio(audio_path)
            start = _LANGUAGE_PROBE_START * SAMPLE_RATE
            if len(audio) > start + _LANGUAGE_PROBE_SECONDS * SAMPLE_RATE:
                audio = audio[start:]
            audio = pad_or_trim(audio, _LANGUAGE_PROBE_SECONDS * SAMPLE_RATE)
            mel = log_mel_spectrogram(audio, n_mels=model.dims.n_mels)
            mel = pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16)
            _, probs = model.detect_language(mel)
            return max(probs, key=probs.get)
        except Exception as e:
            logger.warning("Language detection failed: %s", e)
            return None

    def unload_models(self):
        """Release loaded models to free memory."""
        self._release_whisper()
        self._parakeet = None
        if WHISPER_AVAILABLE:
            mx.clear_cache()
        elif PARAKEET_AVAILABLE:
            import mlx.core

            mlx.core.clear_cache()
        logger.info("Unloaded transcription models")

    def transcribe_with_whisper(
        self, audio_path: str, language: Optional[str] = None
    ) -> Dict[str, Any]:
        """Transcribe audio using Whisper (MLX), in ``language`` if given;
        otherwise Whisper detects it from the first 30 seconds."""
        if not WHISPER_AVAILABLE:
            raise TranscriptionError(
                "No speech-to-text engine is installed: transcription needs "
                "mlx-whisper, which runs only on a Mac with Apple Silicon"
            )
        try:
            result = mlx_whisper.transcribe(
                audio_path,
                path_or_hf_repo=self.whisper_model,
                language=language,
                word_timestamps=True,
                verbose=None,
            )
        except Exception as e:
            raise TranscriptionError(f"Whisper failed: {e}") from e

        return {
            "segments": [
                {
                    "start": segment.get("start", 0),
                    "end": segment.get("end", 0),
                    "text": segment.get("text", "").strip(),
                    "speaker": None,
                    # Whisper reports the probability that a segment holds
                    # no speech; its complement serves as confidence.
                    "confidence": 1.0 - segment.get("no_speech_prob", 0.0),
                }
                for segment in result.get("segments", [])
            ],
            "language": result.get("language"),
        }

    def transcribe_with_parakeet(
        self, audio_path: str, language: Optional[str] = None
    ) -> Dict[str, Any]:
        """Transcribe audio using Nvidia Parakeet MLX. ``language`` is the
        language detected beforehand; Parakeet can't be told it, it is only
        reported back. If Parakeet is missing or fails, Whisper transcribes
        the whole episode instead."""
        if not PARAKEET_AVAILABLE:
            logger.warning("Parakeet MLX not available, falling back to Whisper")
            return self.transcribe_with_whisper(audio_path, language)

        try:
            if self._parakeet is None:
                logger.info("Loading Parakeet model: %s", self.parakeet_model)
                self._parakeet = parakeet_from_pretrained(self.parakeet_model)
            result = self._parakeet.transcribe(
                audio_path,
                chunk_duration=_CHUNK_SECONDS,
                overlap_duration=_CHUNK_OVERLAP_SECONDS,
            )
        except Exception as e:
            logger.error("Parakeet failed, falling back to Whisper: %s", e)
            self._parakeet = None  # only one model in memory at a time
            return self.transcribe_with_whisper(audio_path, language)

        return {
            "segments": [
                {
                    "start": sentence.start,
                    "end": sentence.end,
                    "text": sentence.text.strip(),
                    "speaker": None,  # no speaker diarization
                    "confidence": sentence.confidence,
                }
                for sentence in result.sentences
            ],
            "language": language,
        }

    def transcribe_episode(self, episode_id: int) -> None:
        """Transcribe a single episode and store its transcript and language.

        Raises TranscriptionError, leaving the episode untouched, if it
        can't be transcribed.
        """
        episode = self.db.get_episode_by_id(episode_id)
        if not episode:
            raise TranscriptionError(f"Episode {episode_id} not found")
        if episode["status"] != "downloaded":
            raise TranscriptionError(
                f"Episode {episode_id} is already {episode['status']}"
            )
        audio_path = episode["file_path"]
        if not audio_path or not Path(audio_path).exists():
            raise TranscriptionError(f"Audio file not found: {audio_path}")

        logger.info("Transcribing: %s", episode["title"])

        # Pick the engine by language: Parakeet only for the languages it
        # handles; Whisper for the rest, told the language. If detection
        # failed, Whisper detects it itself rather than risk Parakeet.
        language = self.detect_language(audio_path)
        logger.info("Detected language: %s", language)
        if self.use_parakeet and language in self.parakeet_languages:
            self._release_whisper()  # only one model in memory at a time
            result = self.transcribe_with_parakeet(audio_path, language)
        else:
            result = self.transcribe_with_whisper(audio_path, language)

        segments = [s for s in result["segments"] if s["text"]]
        if not segments:
            raise TranscriptionError("No speech was found in the audio")

        self.db.store_transcript(episode_id, segments, result.get("language"))
        logger.info("Transcribed: %s", episode["title"])
