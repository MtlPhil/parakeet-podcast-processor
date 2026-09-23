"""Tests for the provider-agnostic LLM client helpers (no network calls)."""

import pytest

from p3 import llm
from p3.llm import LLMClient, load_api_key, resolve_provider_and_model


class TestResolveProviderAndModel:
    def test_defaults_to_ollama(self):
        assert resolve_provider_and_model({}) == ("ollama", llm.DEFAULT_OLLAMA_MODEL)

    def test_uses_configured_provider_and_model(self):
        settings = {"llm_provider": "openai", "llm_model": "gpt-4o-mini"}
        assert resolve_provider_and_model(settings) == ("openai", "gpt-4o-mini")

    def test_gemini_uses_gemini_model_setting(self):
        settings = {
            "llm_provider": "ollama",
            "llm_model": "llama3.2:latest",
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
