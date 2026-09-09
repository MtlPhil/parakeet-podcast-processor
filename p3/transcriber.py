"""Audio transcription using Whisper and Parakeet."""

import os
import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Any
import whisper

from .database import P3Database

logger = logging.getLogger(__name__)

# Optional Parakeet MLX support
try:
    from parakeet_mlx import from_pretrained as parakeet_from_pretrained
    PARAKEET_AVAILABLE = True
except ImportError:
    PARAKEET_AVAILABLE = False


class AudioTranscriber:
    def __init__(self, db: P3Database, whisper_model: str = "base",
                 use_parakeet: bool = False, parakeet_model: str = "mlx-community/parakeet-tdt-0.6b-v2"):
        self.db = db
        self.whisper_model = whisper_model
        self.use_parakeet = use_parakeet
        self.parakeet_model = parakeet_model
        self._whisper = None
        self._parakeet = None

    def _load_whisper(self):
        """Lazy load Whisper model."""
        if self._whisper is None:
            logger.info("Loading Whisper model: %s", self.whisper_model)
            self._whisper = whisper.load_model(self.whisper_model)

    def _load_parakeet(self):
        """Lazy load Parakeet MLX model."""
        if self._parakeet is None and PARAKEET_AVAILABLE:
            logger.info("Loading Parakeet model: %s", self.parakeet_model)
            self._parakeet = parakeet_from_pretrained(self.parakeet_model)

    def unload_models(self):
        """Release loaded models to free memory."""
        self._whisper = None
        self._parakeet = None
        logger.info("Unloaded transcription models")

    def transcribe_with_whisper(self, audio_path: str) -> Optional[Dict[str, Any]]:
        """Transcribe audio using OpenAI Whisper."""
        self._load_whisper()

        try:
            result = self._whisper.transcribe(
                audio_path,
                word_timestamps=True,
                verbose=False
            )

            segments = []
            for segment in result.get('segments', []):
                # no_speech_prob is probability of NO speech — invert for confidence
                no_speech_prob = segment.get('no_speech_prob', 0.0)
                confidence = 1.0 - no_speech_prob

                segments.append({
                    'start': segment.get('start', 0),
                    'end': segment.get('end', 0),
                    'text': segment.get('text', '').strip(),
                    'speaker': None,
                    'confidence': confidence
                })

            return {
                'segments': segments,
                'language': result.get('language'),
                'text': result.get('text', ''),
                'provider': 'whisper'
            }

        except Exception as e:
            logger.error("Whisper transcription failed: %s", e)
            return None

    def transcribe_with_parakeet(self, audio_path: str) -> Optional[Dict[str, Any]]:
        """Transcribe audio using Nvidia Parakeet MLX."""
        if not PARAKEET_AVAILABLE:
            logger.warning("Parakeet MLX not available, falling back to Whisper")
            return self.transcribe_with_whisper(audio_path)

        self._load_parakeet()

        try:
            # Split long audio into 10-minute segments so Parakeet MLX can
            # process hour-long episodes without exhausting memory.
            temp_dir = tempfile.mkdtemp(prefix="p3_chunks_")
            base_name = Path(audio_path).stem
            output_pattern = os.path.join(temp_dir, f"{base_name}_%03d.wav")

            logger.info("Splitting audio into 10-minute segments")
            try:
                subprocess.run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-i", audio_path,
                    "-f", "segment",
                    "-segment_time", "600",
                    "-c", "copy",
                    output_pattern
                ], check=True)
            except subprocess.CalledProcessError as e:
                logger.error("Audio splitting failed: %s", e)
                return self.transcribe_with_whisper(audio_path)

            chunks = sorted(Path(temp_dir).glob("*.wav"))
            logger.info("Created %d chunks", len(chunks))

            if not chunks:
                logger.warning("No chunks created for %s", audio_path)
                return None

            segments = []
            text_parts = []
            for i, chunk in enumerate(chunks, start=1):
                logger.info("Transcribing chunk %d/%d", i, len(chunks))
                offset = 600 * (i - 1)
                try:
                    result = self._parakeet.transcribe(str(chunk))
                    for sentence in result.sentences:
                        segments.append({
                            'start': offset + sentence.start,
                            'end': offset + sentence.end,
                            'text': sentence.text.strip(),
                            'speaker': None,  # Parakeet doesn't do speaker identification
                            'confidence': 1.0,  # Parakeet doesn't provide confidence scores
                        })
                    text_parts.append(result.text)
                except Exception as e:
                    logger.warning("Transcription failed for chunk %d: %s", i, e)

            # Clean up temporary chunk files
            for chunk in chunks:
                try:
                    os.remove(chunk)
                except OSError:
                    pass
            try:
                os.rmdir(temp_dir)
            except OSError:
                pass

            return {
                'segments': segments,
                'language': 'en',  # Parakeet is English-only
                'text': ' '.join(text_parts),
                'provider': 'parakeet-mlx'
            }

        except Exception as e:
            logger.error("Parakeet transcription failed: %s", e)
            logger.info("Falling back to Whisper")
            return self.transcribe_with_whisper(audio_path)
      
    def transcribe_episode(self, episode_id: int) -> bool:
        """Transcribe a single episode and store results."""
        episode = self.db.get_episode_by_id(episode_id)

        if not episode:
            logger.warning("Episode %d not found", episode_id)
            return False

        if episode['status'] != 'downloaded':
            logger.warning("Episode %d has status '%s', expected 'downloaded'",
                           episode_id, episode['status'])
            return False

        if not episode['file_path'] or not Path(episode['file_path']).exists():
            logger.error("Audio file not found: %s", episode.get('file_path'))
            return False

        logger.info("Transcribing: %s", episode['title'])

        # Choose transcription method
        if self.use_parakeet:
            result = self.transcribe_with_parakeet(episode['file_path'])
        else:
            result = self.transcribe_with_whisper(episode['file_path'])

        if not result:
            return False

        # Store transcript segments in database
        self.db.add_transcript_segments(episode_id, result['segments'])

        # Update episode status
        self.db.update_episode_status(episode_id, 'transcribed')

        logger.info("Transcribed: %s", episode['title'])
        return True

    def transcribe_all_pending(self) -> int:
        """Transcribe all episodes with 'downloaded' status."""
        episodes = self.db.get_episodes_by_status('downloaded')
        transcribed_count = 0

        for episode in episodes:
            if self.transcribe_episode(episode['id']):
                transcribed_count += 1

        return transcribed_count

    def get_full_transcript(self, episode_id: int) -> str:
        """Get the full transcript text for an episode."""
        segments = self.db.get_transcripts_for_episode(episode_id)
        return "\n".join(segment['text'] for segment in segments)

    def export_transcript(self, episode_id: int, format: str = "txt") -> str:
        """Export transcript in various formats."""
        segments = self.db.get_transcripts_for_episode(episode_id)

        if format == "txt":
            return "\n".join(segment['text'] for segment in segments)

        elif format == "srt":
            srt_content = []
            for i, segment in enumerate(segments, 1):
                start_time = self._seconds_to_srt_time(segment['timestamp_start'] or 0)
                end_time = self._seconds_to_srt_time(segment['timestamp_end'] or 0)
                srt_content.append(f"{i}\n{start_time} --> {end_time}\n{segment['text']}\n")
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

