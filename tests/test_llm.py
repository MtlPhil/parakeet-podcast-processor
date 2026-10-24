"""Tests for the provider-agnostic LLM client helpers (no network calls)."""

from types import SimpleNamespace

import pytest

from p3 import llm
from p3.llm import (
    LLMClient,
    load_api_key,
    release_ollama_models,
    resolve_provider_and_model,
)


class TestResolveProviderAndModel:
    def test_defaults_to_ollama(self):
        assert resolve_provider_and_model({}) == ("ollama", llm.DEFAULT_OLLAMA_MODEL)

    def test_uses_configured_provider_and_model(self):
        settings = {"llm_provider": "openai", "llm_model": "gpt-4o-mini"}
        assert resolve_provider_and_model(settings) == ("openai", "gpt-4o-mini")

    def test_gemini_uses_gemini_model_setting(self):
        settings = {
            "llm_provider": "ollama",
            "llm_model": "gemma4:12b",
            "gemini_model": "gemini-test",
        }
        assert resolve_provider_and_model(settings, provider="gemini") == (
            "gemini",
            "gemini-test",
        )

    def test_explicit_model_wins(self):
        settings = {"llm_provider": "gemini", "gemini_model": "gemini-test"}
        assert resolve_provider_and_model(settings, model="custom") == (
            "gemini",
            "custom",
        )


class TestLoadApiKey:
    def test_ollama_needs_no_key(self):
        assert load_api_key("ollama") is None

    def test_openai_reads_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert load_api_key("openai") == "sk-test"

    def test_gemini_env_takes_precedence_over_file(self, monkeypatch, tmp_path):
        key_file = tmp_path / "gemini_api_key.txt"
        key_file.write_text("from-file\n")
        monkeypatch.setattr(llm, "GEMINI_API_KEY_FILE", key_file)

        monkeypatch.setenv("GEMINI_API_KEY", "from-env")
        assert load_api_key("gemini") == "from-env"

        monkeypatch.delenv("GEMINI_API_KEY")
        assert load_api_key("gemini") == "from-file"

    def test_gemini_missing_key(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setattr(llm, "GEMINI_API_KEY_FILE", tmp_path / "missing.txt")
        assert load_api_key("gemini") is None


class TestLLMClient:
    def test_ollama_is_always_configured(self):
        assert LLMClient(provider="ollama").is_configured

    def test_hosted_provider_without_key_is_not_configured(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert not LLMClient(provider="openai").is_configured

    def test_unsupported_provider_raises(self):
        with pytest.raises(ValueError, match="Unsupported LLM provider"):
            LLMClient(provider="nonexistent").chat("system", "user")

    def test_openai_without_key_raises_before_network(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            LLMClient(provider="openai").chat("system", "user")


class _FakeOllama:
    """Stands in for ollama.Client: reports ``loaded`` until a model's
    unload request has been answered by ``polls_until_gone`` more ps() calls."""

    def __init__(self, loaded, polls_until_gone=1, unreachable=False):
        self.loaded = list(loaded)
        self.polls_until_gone = polls_until_gone
        self.unreachable = unreachable
        self.unloaded = []
        self._polls_after_unload = 0

    def ps(self):
        if self.unreachable:
            raise ConnectionError("Failed to connect to Ollama")
        if self.unloaded:
            self._polls_after_unload += 1
            if self._polls_after_unload > self.polls_until_gone:
                self.loaded = []
        return SimpleNamespace(models=[SimpleNamespace(model=m) for m in self.loaded])

    def generate(self, model, keep_alive=None):
        assert keep_alive == 0
        self.unloaded.append(model)


class TestReleaseOllamaModels:
    @pytest.fixture
    def fake(self, monkeypatch):
        def install(**kwargs):
            client = _FakeOllama(**kwargs)
            monkeypatch.setattr(llm, "OLLAMA_AVAILABLE", True)
            monkeypatch.setattr(
                llm, "ollama", SimpleNamespace(Client=lambda host: client)
            )
            monkeypatch.setattr(llm.time, "sleep", lambda s: None)
            return client

        return install

    def test_unloads_every_model_and_waits_until_gone(self, fake):
        client = fake(loaded=["gemma4:12b", "other-model:latest"], polls_until_gone=3)
        release_ollama_models()
        assert client.unloaded == ["gemma4:12b", "other-model:latest"]
        assert client.loaded == []

    def test_nothing_loaded_sends_no_unload(self, fake):
        client = fake(loaded=[])
        release_ollama_models()
        assert client.unloaded == []

    def test_stopped_ollama_holds_nothing(self, fake):
        client = fake(loaded=[], unreachable=True)
        release_ollama_models()
        assert client.unloaded == []

    def test_raises_when_a_model_stays_loaded(self, fake, monkeypatch):
        fake(loaded=["gemma4:12b"], polls_until_gone=10**9)
        clock = iter(range(0, 10**6, 10))
        monkeypatch.setattr(llm.time, "monotonic", lambda: next(clock))
        with pytest.raises(RuntimeError, match="gemma4:12b"):
            release_ollama_models(timeout=60)


class TestGeminiRetries:
    """Gemini's transient failures are retried; the client is a stub."""

    def _client(self, monkeypatch, outcomes):
        sleeps = []
        calls = []

        class FakeClient:
            def __init__(self, timeout):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def post(self, url, headers, json):
                calls.append(url)
                outcome = outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                status, text = outcome
                return SimpleNamespace(
                    status_code=status,
                    text=text,
                    json=lambda: {
                        "candidates": [{"content": {"parts": [{"text": text}]}}]
                    },
                )

        monkeypatch.setattr(llm.httpx, "Client", FakeClient)
        monkeypatch.setattr(llm.time, "sleep", sleeps.append)
        client = LLMClient(provider="gemini", model="gemini-test", api_key="k")
        return client, calls, sleeps

    def test_overload_and_timeout_are_retried(self, monkeypatch):
        import httpx

        client, calls, sleeps = self._client(
            monkeypatch,
            [(503, "overloaded"), httpx.ReadTimeout("timed out"), (200, " notes ")],
        )
        assert client.chat("s", "u") == "notes"
        assert len(calls) == 3
        assert sleeps == list(llm._GEMINI_RETRY_WAITS[:2])

    def test_gives_up_after_the_last_retry(self, monkeypatch):
        attempts = len(llm._GEMINI_RETRY_WAITS) + 1
        client, calls, _ = self._client(monkeypatch, [(503, "overloaded")] * attempts)
        with pytest.raises(RuntimeError, match="503"):
            client.chat("s", "u")
        assert len(calls) == attempts

    def test_a_client_error_is_not_retried(self, monkeypatch):
        client, calls, sleeps = self._client(monkeypatch, [(400, "bad request")])
        with pytest.raises(RuntimeError, match="400"):
            client.chat("s", "u")
        assert len(calls) == 1 and sleeps == []
