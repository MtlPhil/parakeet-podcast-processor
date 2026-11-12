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
        monkeypatch.setenv("P3_HOME", str(tmp_path))
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "gemini_api_key.txt").write_text("from-file\n")

        monkeypatch.setenv("GEMINI_API_KEY", "from-env")
        assert load_api_key("gemini") == "from-env"

        monkeypatch.delenv("GEMINI_API_KEY")
        assert load_api_key("gemini") == "from-file"

    def test_gemini_missing_key(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("P3_HOME", str(tmp_path))
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

    @pytest.fixture(autouse=True)
    def _no_skipped_models(self, monkeypatch):
        monkeypatch.setattr(llm, "_gemini_skipped_until", {})

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

    def test_falls_back_after_the_last_retry(self, monkeypatch):
        attempts = len(llm._GEMINI_RETRY_WAITS) + 1
        client, calls, _ = self._client(
            monkeypatch, [(503, "overloaded")] * attempts + [(200, "notes")]
        )
        assert client.chat("s", "u") == "notes"
        assert len(calls) == attempts + 1
        assert llm.GEMINI_FALLBACK_MODEL in calls[-1]
        assert client.model == llm.GEMINI_FALLBACK_MODEL

    def test_keeps_the_fallback_for_later_requests(self, monkeypatch):
        attempts = len(llm._GEMINI_RETRY_WAITS) + 1
        client, calls, _ = self._client(
            monkeypatch,
            [(503, "overloaded")] * attempts + [(200, "one"), (200, "two")],
        )
        client.chat("s", "u")
        assert client.chat("s", "u") == "two"
        assert llm.GEMINI_FALLBACK_MODEL in calls[-1]

    def test_other_clients_skip_the_model_for_a_while(self, monkeypatch):
        attempts = len(llm._GEMINI_RETRY_WAITS) + 1
        client, calls, _ = self._client(
            monkeypatch,
            [(503, "overloaded")] * attempts + [(200, "one"), (200, "two")],
        )
        client.chat("s", "u")
        # A new client (the next Ask question, the next job) goes straight
        # to the fallback, with no retries.
        later = LLMClient(provider="gemini", model="gemini-test", api_key="k")
        assert later.chat("s", "u") == "two"
        assert len(calls) == attempts + 2
        assert llm.GEMINI_FALLBACK_MODEL in calls[-1]

    def test_the_model_is_tried_again_once_the_time_is_up(self, monkeypatch):
        attempts = len(llm._GEMINI_RETRY_WAITS) + 1
        client, calls, _ = self._client(
            monkeypatch,
            [(503, "overloaded")] * attempts + [(200, "one"), (200, "two")],
        )
        client.chat("s", "u")
        now = llm.time.monotonic()
        monkeypatch.setattr(
            llm.time, "monotonic", lambda: now + llm._GEMINI_SKIP_SECONDS + 1
        )
        later = LLMClient(provider="gemini", model="gemini-test", api_key="k")
        assert later.chat("s", "u") == "two"
        assert "gemini-test" in calls[-1]

    def test_falls_back_when_unreachable(self, monkeypatch):
        import httpx

        attempts = len(llm._GEMINI_RETRY_WAITS) + 1
        client, calls, _ = self._client(
            monkeypatch,
            [httpx.ConnectError("down")] * attempts + [(200, "notes")],
        )
        assert client.chat("s", "u") == "notes"
        assert llm.GEMINI_FALLBACK_MODEL in calls[-1]

    def test_gives_up_when_the_fallback_fails_too(self, monkeypatch):
        attempts = len(llm._GEMINI_RETRY_WAITS) + 1
        client, calls, _ = self._client(
            monkeypatch, [(503, "overloaded")] * attempts * 2
        )
        with pytest.raises(RuntimeError, match="503"):
            client.chat("s", "u")
        assert len(calls) == attempts * 2

    def test_no_fallback_from_the_fallback_model(self, monkeypatch):
        attempts = len(llm._GEMINI_RETRY_WAITS) + 1
        client, calls, _ = self._client(monkeypatch, [(503, "overloaded")] * attempts)
        client.model = llm.GEMINI_FALLBACK_MODEL
        with pytest.raises(RuntimeError, match="503"):
            client.chat("s", "u")
        assert len(calls) == attempts

    def test_a_client_error_is_not_retried(self, monkeypatch):
        client, calls, sleeps = self._client(monkeypatch, [(400, "bad request")])
        with pytest.raises(RuntimeError, match="400"):
            client.chat("s", "u")
        assert len(calls) == 1 and sleeps == []
        assert client.model == "gemini-test"


class _Recorder:
    """Stands in for httpx.Client: records the request, replies as told."""

    def __init__(self, monkeypatch, status=200, body=None):
        recorder = self
        self.status, self.body = status, body or {}

        class Response:
            status_code = status
            text = "error text"

            def json(self_inner):
                return recorder.body

        class Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def post(self, url, headers=None, json=None):
                recorder.url, recorder.headers, recorder.json = url, headers, json
                return Response()

        monkeypatch.setattr(llm.httpx, "Client", Client)


class TestOpenAICompatibleServer:
    _REPLY = {"choices": [{"message": {"content": " hi "}}]}

    def test_openai_itself_by_default(self, monkeypatch):
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        seen = _Recorder(monkeypatch, body=self._REPLY)
        assert LLMClient("openai", "gpt-x", api_key="sk").chat("s", "u") == "hi"
        assert seen.url == "https://api.openai.com/v1/chat/completions"
        assert seen.headers == {"Authorization": "Bearer sk"}

    def test_another_server_needs_no_key(self, monkeypatch, tmp_path):
        monkeypatch.setenv("P3_HOME", str(tmp_path))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:1234/v1/")
        seen = _Recorder(monkeypatch, body=self._REPLY)
        client = LLMClient("openai", "local-model")
        assert client.is_configured
        assert client.chat("s", "u") == "hi"
        assert seen.url == "http://localhost:1234/v1/chat/completions"
        assert seen.headers == {}


class TestAnthropic:
    def test_a_plain_request(self, monkeypatch):
        seen = _Recorder(
            monkeypatch,
            body={
                "content": [
                    {"type": "thinking", "thinking": ""},
                    {"type": "text", "text": " Bonjour. "},
                ]
            },
        )
        client = LLMClient("anthropic", "claude-haiku-5-5", api_key="key")
        assert client.chat("system", "user", max_tokens=300) == "Bonjour."
        assert seen.url == "https://api.anthropic.com/v1/messages"
        assert seen.headers == {"x-api-key": "key", "anthropic-version": "2026-10-21"}
        assert seen.json == {
            "model": "claude-haiku-5-5",
            "max_tokens": 300,
            "system": "system",
            "messages": [{"role": "user", "content": "user"}],
        }

    def test_errors_and_a_missing_key_are_reported(self, monkeypatch, tmp_path):
        monkeypatch.setenv("P3_HOME", str(tmp_path))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="No Anthropic API key"):
            LLMClient("anthropic", "m").chat("s", "u")
        _Recorder(monkeypatch, status=529)
        with pytest.raises(RuntimeError, match="Anthropic API error: 529"):
            LLMClient("anthropic", "m", api_key="key").chat("s", "u")

    def test_the_shared_ollama_model_is_not_sent_to_anthropic(self):
        settings = {"llm_provider": "ollama", "llm_model": "gemma4:12b"}
        assert resolve_provider_and_model(settings, provider="anthropic") == (
            "anthropic",
            llm.DEFAULT_ANTHROPIC_MODEL,
        )
        settings = {"llm_provider": "anthropic", "llm_model": "claude-sonnet-5-5"}
        assert resolve_provider_and_model(settings)[1] == "claude-sonnet-5-5"
