"""Provider-agnostic chat client shared by the summarizer and the writer."""

import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

import httpx

try:
    import ollama

    OLLAMA_AVAILABLE = True
except ImportError:
    OLLAMA_AVAILABLE = False

SUPPORTED_PROVIDERS = ("ollama", "openai", "gemini")

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "llama3.2:latest"
DEFAULT_GEMINI_MODEL = "gemini-flash-3.6"

# Optional on-disk location for the Gemini API key (gitignored). The
# GEMINI_API_KEY environment variable takes precedence.
GEMINI_API_KEY_FILE = Path("config/gemini_api_key.txt")

_REQUEST_TIMEOUT = 120.0


def load_api_key(provider: str) -> Optional[str]:
    """Look up the API key for a hosted provider, or None if unset."""
    if provider == "openai":
        return os.getenv("OPENAI_API_KEY")
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
            options={
                # Hard cap on output length so a small model caught in a
                # repetition loop cannot generate until the context is full.
                "num_predict": max_tokens,
                # Slightly above Ollama's 1.1 default to discourage such loops.
                "repeat_penalty": 1.3,
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
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
            response = client.post(
                url,
                # Sent as a header rather than a query parameter so the key
                # never appears in request logs.
                headers={"x-goog-api-key": self.api_key},
                json={
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {
                        "maxOutputTokens": max_tokens,
                        "temperature": 0.2,
                    },
                },
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
