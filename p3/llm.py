"""Provider-agnostic chat client shared by the summarizer and the writer."""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple, Union

import httpx

try:
    import ollama

    OLLAMA_AVAILABLE = True
except ImportError:
    OLLAMA_AVAILABLE = False

logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = ("ollama", "openai", "gemini")

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "gemma4:12b"
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
# Used instead when the configured Gemini model stays overloaded or
# unreachable through every retry.
GEMINI_FALLBACK_MODEL = "gemini-3.6-flash"

# Context window requested from Ollama: the longest cleaned/truncated
# transcript (~100k characters) is about 25k tokens, plus the prompt.
_OLLAMA_NUM_CTX = 32768

# Optional on-disk location for the Gemini API key (gitignored). The
# GEMINI_API_KEY environment variable takes precedence.
GEMINI_API_KEY_FILE = Path("config/gemini_api_key.txt")
# Same for OpenAI: OPENAI_API_KEY takes precedence over this file.
OPENAI_API_KEY_FILE = Path("config/openai_api_key.txt")

_REQUEST_TIMEOUT = 120.0

# Gemini answers 503 (and sometimes 429/5xx or nothing at all) while it is
# overloaded; such a request is retried after each of these waits (seconds).
_GEMINI_RETRY_WAITS = (5.0, 15.0, 45.0)
_GEMINI_RETRY_STATUSES = {429, 500, 502, 503, 504}

# How long release_ollama_models waits for Ollama to free a model's memory.
_OLLAMA_RELEASE_TIMEOUT = 60.0


class _GeminiUnavailable(RuntimeError):
    """Gemini stayed overloaded or unreachable through every retry."""


def load_api_key(provider: str) -> Optional[str]:
    """Look up the API key for a hosted provider, or None if unset."""
    if provider == "openai":
        key = os.getenv("OPENAI_API_KEY")
        if not key and OPENAI_API_KEY_FILE.exists():
            key = OPENAI_API_KEY_FILE.read_text().strip()
        return key or None
    if provider == "gemini":
        key = os.getenv("GEMINI_API_KEY")
        if not key and GEMINI_API_KEY_FILE.exists():
            key = GEMINI_API_KEY_FILE.read_text().strip()
        return key or None
    return None


def resolve_provider_and_model(
    settings: Dict[str, Any],
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> Tuple[str, str]:
    """Pick the (provider, model) pair for a request.

    Explicit arguments win. Otherwise the configured ``llm_provider`` is used,
    with ``gemini_model`` as the model for Gemini and ``llm_model`` for
    everything else, since the two are configured independently.
    """
    resolved_provider = (provider or settings.get("llm_provider") or "ollama").lower()
    if model:
        resolved_model = model
    elif resolved_provider == "gemini":
        resolved_model = settings.get("gemini_model") or DEFAULT_GEMINI_MODEL
    else:
        resolved_model = settings.get("llm_model") or DEFAULT_OLLAMA_MODEL
    return resolved_provider, resolved_model


def release_ollama_models(
    base_url: str = DEFAULT_OLLAMA_URL, timeout: float = _OLLAMA_RELEASE_TIMEOUT
) -> None:
    """Unload every model Ollama holds and wait until none is left.

    A digest model kept resident for its ``keep_alive`` window and the
    transcription models don't fit in memory together, so transcription calls
    this first. Raises RuntimeError rather than return while a model is still
    loaded. A stopped Ollama holds nothing.
    """
    if not OLLAMA_AVAILABLE:
        return
    client = ollama.Client(host=base_url)
    try:
        loaded = [m.model for m in client.ps().models if m.model]
    except (ConnectionError, httpx.HTTPError):
        return
    for name in loaded:
        client.generate(model=name, keep_alive=0)
    deadline = time.monotonic() + timeout
    while loaded:
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"Ollama still holds {', '.join(loaded)} after {timeout:.0f}s; "
                "not loading transcription models alongside it"
            )
        time.sleep(0.5)
        loaded = [m.model for m in client.ps().models if m.model]


class LLMClient:
    """Minimal system+user chat interface over Ollama, OpenAI and Gemini."""

    def __init__(
        self,
        provider: str = "ollama",
        model: str = DEFAULT_OLLAMA_MODEL,
        api_key: Optional[str] = None,
        ollama_base_url: str = DEFAULT_OLLAMA_URL,
        keep_alive: Union[str, int] = "30s",
    ):
        self.provider = provider.lower()
        self.model = model
        self.api_key = api_key or load_api_key(self.provider)
        self.ollama_base_url = ollama_base_url
        # How long Ollama keeps the model resident after a call. A short
        # window lets back-to-back calls reuse the loaded model without
        # holding several GB of memory once a batch is done.
        self.keep_alive = keep_alive

    @property
    def is_configured(self) -> bool:
        """True when a real LLM backend can be called (local, or keyed)."""
        return self.provider == "ollama" or bool(self.api_key)

    def chat(self, system: str, user: str, max_tokens: int = 2000) -> str:
        """Send one system+user exchange and return the reply text."""
        if self.provider == "ollama":
            return self._chat_ollama(system, user, max_tokens)
        if self.provider == "openai":
            return self._chat_openai(system, user, max_tokens)
        if self.provider == "gemini":
            return self._chat_gemini(system, user, max_tokens)
        raise ValueError(
            f"Unsupported LLM provider: {self.provider!r} "
            f"(expected one of {', '.join(SUPPORTED_PROVIDERS)})"
        )

    def chat_stream(
        self, system: str, user: str, max_tokens: int = 2000
    ) -> Iterator[str]:
        """Like ``chat``, but yields the reply as it is generated. Ollama and
        Gemini stream; OpenAI yields the whole reply at once. A Gemini stream
        that fails before its first piece falls back to ``chat`` (with its
        retries and fallback model)."""
        if self.provider == "ollama":
            yield from self._stream_ollama(system, user, max_tokens)
        elif self.provider == "gemini":
            yield from self._stream_gemini(system, user, max_tokens)
        else:
            yield self.chat(system, user, max_tokens)

    def _ollama_options(self, max_tokens: int) -> Dict[str, Any]:
        return {
            # Hard cap on output length so a small model caught in a
            # repetition loop cannot generate until the context is full.
            "num_predict": max_tokens,
            # Slightly above Ollama's 1.1 default to discourage such loops.
            "repeat_penalty": 1.3,
            # Ollama's default window is smaller than a long transcript
            # prompt; an overflow silently drops the instructions at its start.
            "num_ctx": _OLLAMA_NUM_CTX,
        }

    def _stream_ollama(self, system: str, user: str, max_tokens: int) -> Iterator[str]:
        if not OLLAMA_AVAILABLE:
            raise RuntimeError("The 'ollama' Python package is not installed")
        client = ollama.Client(host=self.ollama_base_url)
        for part in client.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            keep_alive=self.keep_alive,
            think=False,
            stream=True,
            options=self._ollama_options(max_tokens),
        ):
            text = part["message"]["content"]
            if text:
                yield text

    def _stream_gemini(self, system: str, user: str, max_tokens: int) -> Iterator[str]:
        if not self.api_key:
            raise RuntimeError(
                "No Gemini API key found: set GEMINI_API_KEY or create "
                f"{GEMINI_API_KEY_FILE}"
            )
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:streamGenerateContent?alt=sse"
        )
        started = False
        try:
            with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
                with client.stream(
                    "POST",
                    url,
                    headers={"x-goog-api-key": self.api_key},
                    json={
                        "systemInstruction": {"parts": [{"text": system}]},
                        "contents": [{"role": "user", "parts": [{"text": user}]}],
                        "generationConfig": {
                            "maxOutputTokens": max_tokens,
                            "temperature": 0.2,
                            "thinkingConfig": {"thinkingBudget": 0},
                        },
                    },
                ) as response:
                    if response.status_code != 200:
                        raise RuntimeError(f"Gemini API error: {response.status_code}")
                    for line in response.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = json.loads(line[5:])
                        for candidate in data.get("candidates", []):
                            for piece in candidate.get("content", {}).get("parts", []):
                                if piece.get("text"):
                                    started = True
                                    yield piece["text"]
        except (httpx.TransportError, RuntimeError, ValueError) as e:
            if started:
                raise
            logger.warning("Gemini stream failed (%s); retrying without streaming", e)
            yield self.chat(system, user, max_tokens)

    def _chat_ollama(self, system: str, user: str, max_tokens: int) -> str:
        if not OLLAMA_AVAILABLE:
            raise RuntimeError("The 'ollama' Python package is not installed")
        client = ollama.Client(host=self.ollama_base_url)
        response = client.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            keep_alive=self.keep_alive,
            # Reasoning models would spend the output cap on hidden thoughts.
            think=False,
            options={
                # Hard cap on output length so a small model caught in a
                # repetition loop cannot generate until the context is full.
                "num_predict": max_tokens,
                # Slightly above Ollama's 1.1 default to discourage such loops.
                "repeat_penalty": 1.3,
                # Ollama's default window is smaller than a long transcript
                # prompt; an overflow silently drops the instructions at its start.
                "num_ctx": _OLLAMA_NUM_CTX,
            },
        )
        return response["message"]["content"].strip()

    def _chat_openai(self, system: str, user: str, max_tokens: int) -> str:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
            response = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.2,
                    "max_tokens": max_tokens,
                },
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"OpenAI API error: {response.status_code} - {response.text}"
            )
        return response.json()["choices"][0]["message"]["content"].strip()

    def _chat_gemini(self, system: str, user: str, max_tokens: int) -> str:
        if not self.api_key:
            raise RuntimeError(
                "No Gemini API key found: set GEMINI_API_KEY or create "
                f"{GEMINI_API_KEY_FILE}"
            )
        try:
            return self._gemini_request(self.api_key, system, user, max_tokens)
        except _GeminiUnavailable as e:
            if self.model == GEMINI_FALLBACK_MODEL:
                raise
            # The client keeps the fallback, so the rest of a multi-request
            # job (synopsis sections, translation chunks) doesn't wait out
            # the retries on the overloaded model again.
            logger.warning(
                "%s is unavailable (%s); falling back to %s",
                self.model,
                e,
                GEMINI_FALLBACK_MODEL,
            )
            self.model = GEMINI_FALLBACK_MODEL
            return self._gemini_request(self.api_key, system, user, max_tokens)

    def _gemini_request(
        self, api_key: str, system: str, user: str, max_tokens: int
    ) -> str:
        """One Gemini request to ``self.model``, with the overload retries.
        Raises _GeminiUnavailable once every retry failed that way."""
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )
        for wait in (*_GEMINI_RETRY_WAITS, None):
            try:
                with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
                    response = client.post(
                        url,
                        # Sent as a header rather than a query parameter so the
                        # key never appears in request logs.
                        headers={"x-goog-api-key": api_key},
                        json={
                            "systemInstruction": {"parts": [{"text": system}]},
                            "contents": [{"role": "user", "parts": [{"text": user}]}],
                            "generationConfig": {
                                "maxOutputTokens": max_tokens,
                                "temperature": 0.2,
                                # Thinking tokens count against maxOutputTokens and
                                # can leave the reply empty or cut off.
                                "thinkingConfig": {"thinkingBudget": 0},
                            },
                        },
                    )
            except httpx.TransportError as e:  # timeouts and dropped connections
                if wait is None:
                    raise _GeminiUnavailable(f"Gemini API unreachable: {e}") from e
                logger.warning("Gemini request failed (%s); retrying in %.0fs", e, wait)
                time.sleep(wait)
                continue
            if response.status_code not in _GEMINI_RETRY_STATUSES or wait is None:
                break
            logger.warning(
                "Gemini API returned %d; retrying in %.0fs", response.status_code, wait
            )
            time.sleep(wait)
        if response.status_code in _GEMINI_RETRY_STATUSES:
            raise _GeminiUnavailable(
                f"Gemini API error: {response.status_code} - {response.text}"
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"Gemini API error: {response.status_code} - {response.text}"
            )
        data = response.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Unexpected Gemini response shape: {data}") from e
