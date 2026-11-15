"""The shared settings (the ``settings:`` section of config/feeds.yaml) and
what each is when the file doesn't say.

These cover work done once per episode for everyone. A user's own settings
(language, tone, daily run) are in ``p3.api.deps.USER_SETTING_DEFAULTS``.
"""

import os
from typing import Any, Mapping

# P3_OLLAMA_URL gives another default, read at startup: the container sets
# it, since its own localhost is not the machine Ollama runs on.
DEFAULT_OLLAMA_URL = os.environ.get("P3_OLLAMA_URL") or "http://localhost:11434"
DEFAULT_PARAKEET_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"
DEFAULT_WHISPER_MODEL = "turbo"

SHARED_DEFAULTS: dict[str, Any] = {
    # Downloads
    "max_episodes_per_feed": 10,  # newest episodes taken by each fetch
    "audio_format": "wav",  # wav or mp3
    # Transcription: Parakeet for parakeet_languages, Whisper for the rest
    "parakeet_enabled": False,
    "parakeet_model": DEFAULT_PARAKEET_MODEL,
    "parakeet_languages": ["en"],
    "whisper_model": DEFAULT_WHISPER_MODEL,
    # Unload Ollama's models first, so they never share memory with these
    "release_ollama_before_transcription": True,
    # Speakers (p3/speakers.py): told apart after transcription when the
    # "speakers" extra is installed, then named at the digest step by this
    # provider's LLM ("" = the digest's own; it reads the show notes and the
    # first minutes of the transcript)
    "diarization_enabled": True,
    "diarization_model": "pyannote/speaker-diarization-community-1",
    "speaker_naming_provider": "",
    # Ask's second source, asked per question: "" (off), "gemini" (Google
    # Search through Gemini) or "searxng" (the server at searxng_url).
    "web_search": "",
    "searxng_url": "",
    # The LLM (its model has a default per provider, see p3.llm)
    "ollama_base_url": DEFAULT_OLLAMA_URL,
}


def setting(settings: Mapping[str, Any], key: str) -> Any:
    """A shared setting's value, or its default when ``settings`` has none."""
    value = settings.get(key)
    return SHARED_DEFAULTS[key] if value is None else value
