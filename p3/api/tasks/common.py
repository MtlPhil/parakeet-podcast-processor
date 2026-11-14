"""What the job modules share: the shared settings, the summarizing
cleaner and indexing a digested episode for Ask."""

import logging
from typing import Optional

from p3.api.deps import load_config
from p3.llm import resolve_provider_and_model
from p3.settings import setting

logger = logging.getLogger(__name__)


def _get_settings() -> dict:
    """The shared settings: transcription and digest run once per episode,
    whoever's library it is in."""
    config = load_config()
    return config.get("settings", {})


def _make_embedder(settings: dict):
    from p3.embedder import DEFAULT_EMBEDDING_MODEL, Embedder

    return Embedder(
        settings.get("embedding_model") or DEFAULT_EMBEDDING_MODEL,
        setting(settings, "ollama_base_url"),
    )


def _index_episode(db, episode_id: int, settings: dict) -> str:
    """Add a digested episode to the Ask index. Never raises: when embedding
    fails the passages are kept for keyword search, and a later "Index
    library" run embeds them. Returns a note for the job message."""
    from p3.indexer import index_episode

    try:
        count = index_episode(db, episode_id, _make_embedder(settings))
        return f"{count} passages indexed"
    except Exception as e:
        logger.warning("Embedding episode %d failed: %s", episode_id, e)
    try:
        index_episode(db, episode_id, None)
        return "indexed for keyword search only (embedding failed)"
    except Exception as e:
        logger.exception("Indexing episode %d failed", episode_id)
        return f"not indexed: {e}"


def _make_cleaner(
    db, settings: dict, provider: Optional[str] = None, model: Optional[str] = None
):
    from p3.cleaner import TranscriptCleaner
    from p3.speakers import naming_llm

    llm_provider, llm_model = resolve_provider_and_model(settings, provider, model)
    return TranscriptCleaner(
        db=db,
        llm_provider=llm_provider,
        llm_model=llm_model,
        ollama_base_url=setting(settings, "ollama_base_url"),
        naming_llm=naming_llm(settings),
    )
