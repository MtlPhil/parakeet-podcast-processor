"""Models behind the Ask index: passage embeddings (Ollama) and the reranker
(a sentence-transformers cross-encoder).

Both load on first use and can be released (``release_ask_models``), which
transcription does before loading its own models: the 16 GB machine can't
hold them all at once.
"""

import importlib.util
import logging
import math
import threading
import time
from typing import Any, List, Optional, Sequence

from .llm import DEFAULT_OLLAMA_URL

try:
    import ollama

    OLLAMA_AVAILABLE = True
except ImportError:  # pragma: no cover - ollama is a core dependency
    OLLAMA_AVAILABLE = False

# Checked without importing: sentence-transformers pulls in PyTorch, which
# the API process should only load when a question is first reranked.
RERANKER_AVAILABLE = importlib.util.find_spec("sentence_transformers") is not None

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "bge-m3"
DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
# Kept short: query-time embeddings are tiny, and the model shouldn't sit in
# memory long after a question.
_EMBED_KEEP_ALIVE = "5m"
_BATCH = 16
# The reranker is dropped after this long without a question.
_RERANKER_IDLE_SECONDS = 600


def _normalize(vector: Sequence[float]) -> List[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


class Embedder:
    """Embeds passages and questions with an Ollama embedding model."""

    def __init__(
        self,
        model: str = DEFAULT_EMBEDDING_MODEL,
        ollama_base_url: str = DEFAULT_OLLAMA_URL,
    ):
        self.model = model
        self.ollama_base_url = ollama_base_url

    def embed(self, texts: List[str]) -> List[List[float]]:
        """Unit-length vectors for ``texts``, in order."""
        if not OLLAMA_AVAILABLE:
            raise RuntimeError("The 'ollama' Python package is not installed")
        client = ollama.Client(host=self.ollama_base_url)
        vectors: List[List[float]] = []
        for i in range(0, len(texts), _BATCH):
            response = client.embed(
                model=self.model,
                input=texts[i : i + _BATCH],
                keep_alive=_EMBED_KEEP_ALIVE,
            )
            vectors.extend(_normalize(v) for v in response["embeddings"])
        return vectors


class Reranker:
    """Scores (question, passage) pairs with a cross-encoder, 0 to 1."""

    def __init__(self, model: str = DEFAULT_RERANKER_MODEL):
        self.model_name = model
        self._model: Any = None
        self._last_used = 0.0
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load(self):
        if not RERANKER_AVAILABLE:
            raise RuntimeError(
                "Reranking needs the 'ask' extra: pip install -e '.[ask]'"
            )
        import torch
        from sentence_transformers import CrossEncoder

        device = "mps" if torch.backends.mps.is_available() else "cpu"
        logger.info("Loading reranker %s on %s", self.model_name, device)
        model = CrossEncoder(self.model_name, device=device, max_length=512)
        if device == "mps":
            model.model.half()
        return model

    def score(self, question: str, passages: List[str]) -> List[float]:
        with self._lock:
            model = self._model or self._load()
            self._model = model
            self._last_used = time.monotonic()
            scores = model.predict([(question, p) for p in passages], batch_size=16)
        return [float(s) for s in scores]

    def release(self, idle_only: bool = False):
        """Drop the model (only if unused for a while, with ``idle_only``)."""
        with self._lock:
            if self._model is None:
                return
            if (
                idle_only
                and time.monotonic() - self._last_used < _RERANKER_IDLE_SECONDS
            ):
                return
            self._model = None
        try:
            import gc

            import torch

            gc.collect()
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except ImportError:
            pass
        logger.info("Released the reranker")


_reranker: Optional[Reranker] = None
_reranker_lock = threading.Lock()


def get_reranker(model: str = DEFAULT_RERANKER_MODEL) -> Optional[Reranker]:
    """The process-wide reranker, or None without the 'ask' extra."""
    global _reranker
    if not RERANKER_AVAILABLE:
        return None
    with _reranker_lock:
        if _reranker is None or _reranker.model_name != model:
            _reranker = Reranker(model)
        return _reranker


def release_ask_models():
    """Free the reranker held by this process (the embedding model lives in
    Ollama and goes with ``release_ollama_models``)."""
    if _reranker is not None:
        _reranker.release()
