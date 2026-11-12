"""The shared settings (the ``settings:`` section of config/feeds.yaml) and
what each is when the file doesn't say.

These cover work done once per episode for everyone. A user's own settings
(language, tone, daily run) are in ``p3.api.deps.USER_SETTING_DEFAULTS``.
"""

from typing import Any, Mapping

DEFAULT_OLLAMA_URL = "http://localhost:11434"
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
    # The LLM (its model has a default per provider, see p3.llm)
    "ollama_base_url": DEFAULT_OLLAMA_URL,
}


def setting(settings: Mapping[str, Any], key: str) -> Any:
    """A shared setting's value, or its default when ``settings`` has none."""
    value = settings.get(key)
    return SHARED_DEFAULTS[key] if value is None else value
