"""Audio transcription using Parakeet MLX or Whisper MLX (Apple Silicon).

Each episode's language is detected first. Parakeet transcribes the languages
it handles reliably (English by default); everything else goes to Whisper,
which is told the language. Parakeet has no language input: on spontaneous
non-English speech it drifts into English that reads like a translation.
"""

import json
import logging
import shutil
import subprocess
import tempfile
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

DEFAULT_PARAKEET_MODEL = "mlx-community/parakeet-tdt-0.6b-v2"

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

# Long episodes are transcribed in fixed-length chunks to bound memory use.
_CHUNK_SECONDS = 600

# Language detection listens to this many seconds of audio, starting a minute
# in so that intro music doesn't decide the result.
_LANGUAGE_PROBE_SECONDS = 30
_LANGUAGE_PROBE_START = 60


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

    def _load_parakeet(self):
        """Lazy load Parakeet MLX model."""
        if self._parakeet is None and PARAKEET_AVAILABLE:
            logger.info("Loading Parakeet model: %s", self.parakeet_model)
            self._parakeet = parakeet_from_pretrained(self.parakeet_model)

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
    ) -> Optional[Dict[str, Any]]:
        """Transcribe audio using Whisper (MLX), in ``language`` if given;
        otherwise Whisper detects it from the first 30 seconds."""
        if not WHISPER_AVAILABLE:
            logger.error("Whisper is unavailable: mlx-whisper is not installed")
            return None

        try:
            result = mlx_whisper.transcribe(
                audio_path,
                path_or_hf_repo=self.whisper_model,
                language=language,
                word_timestamps=True,
                verbose=None,
            )

            segments = []
            for segment in result.get("segments", []):
                # no_speech_prob is probability of NO speech — invert for confidence
                no_speech_prob = segment.get("no_speech_prob", 0.0)
                confidence = 1.0 - no_speech_prob

                segments.append(
                    {
                        "start": segment.get("start", 0),
                        "end": segment.get("end", 0),
                        "text": segment.get("text", "").strip(),
                        "speaker": None,
                        "confidence": confidence,
                    }
                )

            return {
                "segments": segments,
                "language": result.get("language"),
                "text": result.get("text", ""),
                "provider": "whisper",
            }

        except Exception as e:
            logger.error("Whisper transcription failed: %s", e)
            return None

    def transcribe_with_parakeet(
        self, audio_path: str, language: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Transcribe audio using Nvidia Parakeet MLX. ``language`` is the
        language detected beforehand; Parakeet can't be told it, it is only
        reported back (and passed to Whisper if Parakeet fails)."""
        if not PARAKEET_AVAILABLE:
            logger.warning("Parakeet MLX not available, falling back to Whisper")
            return self.transcribe_with_whisper(audio_path, language)

        self._load_parakeet()

        temp_dir = tempfile.mkdtemp(prefix="p3_chunks_")
        try:
            output_pattern = str(Path(temp_dir) / f"{Path(audio_path).stem}_%03d.wav")
            try:
                subprocess.run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        audio_path,
                        "-f",
                        "segment",
                        "-segment_time",
                        str(_CHUNK_SECONDS),
                        "-c",
                        "copy",
                        output_pattern,
                    ],
                    check=True,
                )
            except subprocess.CalledProcessError as e:
                logger.error("Audio splitting failed: %s", e)
                return self.transcribe_with_whisper(audio_path, language)

            chunks = sorted(Path(temp_dir).glob("*.wav"))
            logger.info("Split audio into %d chunk(s)", len(chunks))
            if not chunks:
                logger.warning("No chunks created for %s", audio_path)
                return None

            segments = []
            text_parts = []
            for i, chunk in enumerate(chunks):
                logger.info("Transcribing chunk %d/%d", i + 1, len(chunks))
                offset = _CHUNK_SECONDS * i
                try:
                    result = self._parakeet.transcribe(str(chunk))
                    for sentence in result.sentences:
                        segments.append(
                            {
                                "start": offset + sentence.start,
                                "end": offset + sentence.end,
                                "text": sentence.text.strip(),
                                "speaker": None,  # no speaker diarization
                                "confidence": 1.0,  # Parakeet reports no confidence scores
                            }
                        )
                    text_parts.append(result.text)
                except Exception as e:
                    logger.warning("Transcription failed for chunk %d: %s", i + 1, e)

            return {
                "segments": segments,
                "language": language,
                "text": " ".join(text_parts),
                "provider": "parakeet-mlx",
            }

        except Exception as e:
            logger.error(
                "Parakeet transcription failed, falling back to Whisper: %s", e
            )
            return self.transcribe_with_whisper(audio_path, language)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def transcribe_episode(self, episode_id: int) -> bool:
        """Transcribe a single episode and store results."""
        episode = self.db.get_episode_by_id(episode_id)

        if not episode:
            logger.warning("Episode %d not found", episode_id)
            return False

        if episode["status"] != "downloaded":
            logger.warning(
                "Episode %d has status '%s', expected 'downloaded'",
                episode_id,
                episode["status"],
            )
            return False

        if not episode["file_path"] or not Path(episode["file_path"]).exists():
            logger.error("Audio file not found: %s", episode.get("file_path"))
            return False

        logger.info("Transcribing: %s", episode["title"])

        # Pick the engine by language: Parakeet only for the languages it
        # handles; Whisper for the rest, told the language. If detection
        # failed, Whisper detects it itself rather than risk Parakeet.
        language = self.detect_language(episode["file_path"])
        logger.info("Detected language: %s", language)
        if self.use_parakeet and language in self.parakeet_languages:
            self._release_whisper()  # only one model in memory at a time
            result = self.transcribe_with_parakeet(episode["file_path"], language)
        else:
            result = self.transcribe_with_whisper(episode["file_path"], language)

        if not result:
            return False

        # Store transcript segments in database
        self.db.add_transcript_segments(episode_id, result["segments"])
        self.db.set_episode_language(episode_id, result.get("language"))

        # Update episode status
        self.db.update_episode_status(episode_id, "transcribed")

        logger.info("Transcribed: %s", episode["title"])
        return True

    def transcribe_all_pending(self) -> int:
        """Transcribe all episodes with 'downloaded' status."""
        episodes = self.db.get_episodes_by_status("downloaded")
        transcribed_count = 0

        for episode in episodes:
            if self.transcribe_episode(episode["id"]):
                transcribed_count += 1

        return transcribed_count

    def get_full_transcript(self, episode_id: int) -> str:
        """Get the full transcript text for an episode."""
        segments = self.db.get_transcripts_for_episode(episode_id)
        return "\n".join(segment["text"] for segment in segments)

    def export_transcript(self, episode_id: int, format: str = "txt") -> str:
        """Export transcript in various formats."""
        segments = self.db.get_transcripts_for_episode(episode_id)

        if format == "txt":
            return "\n".join(segment["text"] for segment in segments)

        elif format == "srt":
            srt_content = []
            for i, segment in enumerate(segments, 1):
                start_time = self._seconds_to_srt_time(segment["timestamp_start"] or 0)
                end_time = self._seconds_to_srt_time(segment["timestamp_end"] or 0)
                srt_content.append(
                    f"{i}\n{start_time} --> {end_time}\n{segment['text']}\n"
                )
            return "\n".join(srt_content)

        elif format == "json":
            return json.dumps(segments, indent=2, default=str)

        else:
            raise ValueError(f"Unsupported format: {format}")

    def _seconds_to_srt_time(self, seconds: float) -> str:
        """Convert seconds to SRT timestamp format."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millisecs = int((seconds % 1) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millisecs:03d}"
